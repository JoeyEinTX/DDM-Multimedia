# la_quiniela/simulator.py - La Quiniela cup simulator
#
# A virtual gateway plus 20 betting cups on a pty, so every piece of DevPi
# software can be built and tested with no hardware. The real bridge
# (la_quiniela/) opens the pty's slave end like any serial port and cannot
# tell it from a real gateway.
#
#   cd pi5
#   python -m la_quiniela.simulator --list
#   python -m la_quiniela.simulator --scenario normal --devpi http://localhost:5000 --check http://localhost:5000
#
# Standard library only; never imports the Flask app, main.py or pyserial.
# See pi5/LQ_SIMULATOR.md.

import argparse
import os
import sys

_PI5_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PI5_DIR not in sys.path:
    sys.path.insert(0, _PI5_DIR)

from la_quiniela.sim.link import DEFAULT_LINK, PtyLink  # noqa: E402
from la_quiniela.sim.runner import HttpOperator, Simulator, http_snapshot  # noqa: E402
from la_quiniela.sim.scenarios import DESCRIPTIONS  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m la_quiniela.simulator",
                                 description="Virtual La Quiniela gateway + 20 cups on a pty.")
    ap.add_argument("--list", action="store_true", help="list the scenarios and exit")
    ap.add_argument("--scenario", metavar="NAME", help="run one scenario (default: just power the cups and take commands)")
    ap.add_argument("--seed", type=int, help="make a run repeatable")
    ap.add_argument("--speed", type=float, default=1.0, help="compress the scenario's waits by this factor (cadences stay real-time)")
    ap.add_argument("--ideal", action="store_true", help="scale without noise or overshoot: counts are exact")
    ap.add_argument("--auto-demo", action="store_true", help="emulate a DDM_AUTO_DEMO 1 bench build")
    ap.add_argument("--link", default=DEFAULT_LINK, metavar="PATH", help="stable symlink to the port (default %(default)s)")
    ap.add_argument("--devpi", metavar="URL", help="drive operator steps through DevPi's dev endpoints, e.g. http://localhost:5000")
    ap.add_argument("--check", metavar="URL", help="at the end, compare DevPi's /api/lq/snapshot with the expectation and print PASS or FAIL")
    ap.add_argument("--operator-timeout", type=float, default=120.0, metavar="S", help="fail an operator step after this long (default 120)")
    ap.add_argument("--settle", type=float, default=12.0, metavar="S", help="how long --check waits for DevPi to catch up (default 12)")
    ap.add_argument("--quiet", action="store_true", help="print only operator prompts, the expected results and the check result")
    ap.add_argument("--wire", action="store_true", help="also echo every line sent and received")
    ap.add_argument("--no-stdin", action="store_true", help="do not read commands from stdin")
    ap.add_argument("--duration", type=float, metavar="S", help="with no scenario: stop after this many seconds")
    args = ap.parse_args(argv)

    if args.list:
        for name, desc in DESCRIPTIONS.items():
            print("%-16s %s" % (name, desc))
        return 0
    if args.scenario and args.scenario not in DESCRIPTIONS:
        print("unknown scenario %r; --list shows them" % args.scenario, file=sys.stderr)
        return 2

    link = PtyLink(args.link)
    print("virtual serial port: %s  (symlink %s)" % (link.slave_path, args.link), flush=True)
    print("start the app with: DDM_LQ_SERIAL_PORT=%s DDM_LQ_DEV_ENDPOINTS=1 python main.py" % args.link, flush=True)
    sim = Simulator(link=link, seed=args.seed, ideal=args.ideal, auto_demo=args.auto_demo,
                    speed=args.speed, quiet=args.quiet, wire=args.wire,
                    operator=HttpOperator(args.devpi) if args.devpi else None,
                    operator_timeout=args.operator_timeout, settle_s=args.settle)
    try:
        return sim.run(scenario=args.scenario, stdin_commands=not args.no_stdin,
                       duration=args.duration,
                       check=http_snapshot(args.check) if args.check else None)
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
        return 130
    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
