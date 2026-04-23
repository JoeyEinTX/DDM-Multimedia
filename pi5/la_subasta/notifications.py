# la_subasta/notifications.py - SocketIO broadcasts (push notifications in Phase 5)
#
# Thin wrapper around the shared SocketIO instance so bidding/state/payout
# modules don't need to know about Flask-SocketIO directly.

from typing import Optional, Any

# Set via init_notifications() from the blueprint at app startup.
_socketio = None


def init_notifications(socketio) -> None:
    """Register the shared SocketIO instance for broadcasts."""
    global _socketio
    _socketio = socketio


def emit(event: str, payload: dict, room: Optional[str] = None) -> None:
    """Emit a SocketIO event to all clients (or a specific room)."""
    if _socketio is None:
        return
    if room:
        _socketio.emit(event, payload, room=room)
    else:
        _socketio.emit(event, payload)


# --- Convenience wrappers for the named events in the spec -------------------

def bid_placed(horse_id: int, bidder_id: int, bidder_identity: str,
               amount: float, previous_bidder_id: Optional[int],
               previous_bidder_identity: Optional[str]) -> None:
    emit("bid_placed", {
        "horse_id": horse_id,
        "bidder_id": bidder_id,
        "bidder_identity": bidder_identity,
        "amount": amount,
        "previous_bidder_id": previous_bidder_id,
        "previous_bidder_identity": previous_bidder_identity,
    })


def outbid(horse_id: int, old_bidder_id: int, old_bidder_identity: str,
           new_bidder_id: int, new_bidder_identity: str,
           amount: float) -> None:
    emit("outbid", {
        "horse_id": horse_id,
        "old_bidder_id": old_bidder_id,
        "old_bidder_identity": old_bidder_identity,
        "new_bidder_id": new_bidder_id,
        "new_bidder_identity": new_bidder_identity,
        "amount": amount,
    })


def auction_locked(timestamp: float) -> None:
    emit("auction_locked", {"timestamp": timestamp})


def horse_scratched(horse_id: int) -> None:
    emit("horse_scratched", {"horse_id": horse_id})


def results_entered(win: int, place: int, show: int) -> None:
    emit("results_entered", {"win": win, "place": place, "show": show})


def payout_computed(payload: Any) -> None:
    emit("payout_computed", payload)


def paid_marked(bidder_id: int) -> None:
    emit("paid_marked", {"bidder_id": bidder_id})


def bid_voided(bid_id: int, horse_id: int, new_high_bid: Optional[dict]) -> None:
    emit("bid_voided", {
        "bid_id": bid_id,
        "horse_id": horse_id,
        "new_high_bid": new_high_bid,
    })


def settings_changed(key: str, old_value: Any, new_value: Any,
                     changed_at: Optional[str] = None) -> None:
    emit("settings_changed", {
        "key": key,
        "old_value": old_value,
        "new_value": new_value,
        "changed_at": changed_at,
    })
