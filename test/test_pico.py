#!/usr/bin/env python3
"""
Test script for DDM Cup LED Controller (Pico 2 W)
Run this from any computer on the same network to test the Pico controller
"""

import socket
import time
import sys


class PicoTester:
    """Simple test client for Pico LED controller"""
    
    def __init__(self, pico_ip, pico_port=5005):
        self.pico_ip = pico_ip
        self.pico_port = pico_port
    
    def send_command(self, command):
        """Send a command to the Pico and return response"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)  # 5 second timeout
            s.connect((self.pico_ip, self.pico_port))
            s.send(command.encode('utf-8'))
            response = s.recv(1024).decode('utf-8')
            s.close()
            return response
        except Exception as e:
            return f"ERROR: {str(e)}"
    
    def test_connection(self):
        """Test basic connection with PING"""
        print("\n=== Test 1: Connection Test ===")
        print(f"Sending: PING")
        response = self.send_command("PING")
        print(f"Response: {response}")
        
        if response == "PONG":
            print("✓ Connection test PASSED")
            return True
        else:
            print("✗ Connection test FAILED")
            return False
    
    def test_led_control(self):
        """Test basic LED control"""
        print("\n=== Test 2: LED Control ===")
        
        tests = [
            ("LED:ALL_OFF", "Turn all LEDs off"),
            ("LED:BRIGHTNESS:25", "Set brightness to 25%"),
            ("LED:ALL_ON", "Turn all LEDs on (white)"),
            ("LED:ALL_OFF", "Turn all LEDs off"),
        ]
        
        for cmd, description in tests:
            print(f"\n{description}")
            print(f"Sending: {cmd}")
            response = self.send_command(cmd)
            print(f"Response: {response}")
            
            if "OK" in response:
                print(f"✓ {description} - PASSED")
            else:
                print(f"✗ {description} - FAILED")
            
            time.sleep(1)
    
    def test_colors(self):
        """Test color commands"""
        print("\n=== Test 3: Color Control ===")
        
        colors = [
            ("228B22", "Green"),
            ("FFD700", "Gold"),
            ("DC143C", "Rose"),
            ("FFFFFF", "White"),
        ]
        
        for hex_color, name in colors:
            print(f"\nSetting all LEDs to {name} ({hex_color})")
            cmd = f"LED:COLOR:{hex_color}"
            print(f"Sending: {cmd}")
            response = self.send_command(cmd)
            print(f"Response: {response}")
            
            if "OK" in response:
                print(f"✓ {name} color - PASSED")
            else:
                print(f"✗ {name} color - FAILED")
            
            time.sleep(1.5)
        
        # Turn off
        print("\nTurning off...")
        self.send_command("LED:ALL_OFF")
    
    def test_individual_cups(self):
        """Test individual cup control"""
        print("\n=== Test 4: Individual Cup Control ===")
        
        # Test cups 1, 5, 10, 15, 20
        test_cups = [
            (1, "228B22", "Green"),
            (5, "FFD700", "Gold"),
            (10, "DC143C", "Rose"),
            (15, "C0C0C0", "Silver"),
            (20, "CD7F32", "Bronze"),
        ]
        
        # Clear all first
        self.send_command("LED:ALL_OFF")
        time.sleep(0.5)
        
        for cup_num, hex_color, color_name in test_cups:
            print(f"\nSetting cup {cup_num} to {color_name}")
            cmd = f"LED:CUP:{cup_num}:{hex_color}"
            print(f"Sending: {cmd}")
            response = self.send_command(cmd)
            print(f"Response: {response}")
            
            if "OK" in response:
                print(f"✓ Cup {cup_num} - PASSED")
            else:
                print(f"✗ Cup {cup_num} - FAILED")
            
            time.sleep(0.5)
        
        print("\nLet cups display for 3 seconds...")
        time.sleep(3)
        
        # Clear
        print("\nClearing all cups...")
        self.send_command("RESET")
    
    def test_brightness(self):
        """Test brightness levels"""
        print("\n=== Test 5: Brightness Levels ===")
        
        # Set to gold color
        self.send_command("LED:COLOR:FFD700")
        time.sleep(0.5)
        
        brightness_levels = [10, 25, 50, 75, 100, 50]
        
        for brightness in brightness_levels:
            print(f"\nSetting brightness to {brightness}%")
            cmd = f"LED:BRIGHTNESS:{brightness}"
            print(f"Sending: {cmd}")
            response = self.send_command(cmd)
            print(f"Response: {response}")
            
            if "OK" in response:
                print(f"✓ Brightness {brightness}% - PASSED")
            else:
                print(f"✗ Brightness {brightness}% - FAILED")
            
            time.sleep(1)
        
        # Turn off
        self.send_command("LED:ALL_OFF")
    
    def run_all_tests(self):
        """Run all tests"""
        print("="*60)
        print("DDM Cup LED Controller - Pico Test Suite")
        print("="*60)
        print(f"Target: {self.pico_ip}:{self.pico_port}")
        
        # Test connection first
        if not self.test_connection():
            print("\n❌ Connection failed! Cannot continue with other tests.")
            print("\nTroubleshooting:")
            print("1. Check Pico is powered on")
            print("2. Verify Pico's IP address")
            print("3. Ensure both devices are on same network")
            print("4. Check if Pico's main.py is running")
            return
        
        # Run other tests
        try:
            self.test_led_control()
            self.test_colors()
            self.test_individual_cups()
            self.test_brightness()
            
            print("\n" + "="*60)
            print("✓ All tests completed!")
            print("="*60)
            
        except KeyboardInterrupt:
            print("\n\nTests interrupted by user")
            print("Turning off LEDs...")
            self.send_command("RESET")
        except Exception as e:
            print(f"\n\n❌ Test error: {e}")
            print("Attempting to turn off LEDs...")
            self.send_command("RESET")


def interactive_mode(tester):
    """Interactive command mode"""
    print("\n" + "="*60)
    print("Interactive Mode - Send commands directly to Pico")
    print("="*60)
    print("\nCommands:")
    print("  PING")
    print("  LED:ALL_ON")
    print("  LED:ALL_OFF")
    print("  LED:BRIGHTNESS:XX")
    print("  LED:COLOR:RRGGBB")
    print("  LED:CUP:N:RRGGBB")
    print("  RESET")
    print("  quit - Exit interactive mode")
    print()
    
    while True:
        try:
            cmd = input("Enter command: ").strip()
            
            if cmd.lower() == "quit":
                break
            
            if not cmd:
                continue
            
            response = tester.send_command(cmd)
            print(f"Response: {response}\n")
            
        except KeyboardInterrupt:
            print("\n\nExiting interactive mode...")
            break


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: python test_pico.py <PICO_IP_ADDRESS> [--interactive]")
        print("\nExample:")
        print("  python test_pico.py 192.168.1.100")
        print("  python test_pico.py 192.168.1.100 --interactive")
        sys.exit(1)
    
    pico_ip = sys.argv[1]
    interactive = "--interactive" in sys.argv or "-i" in sys.argv
    
    tester = PicoTester(pico_ip)
    
    if interactive:
        # Just run connection test, then enter interactive mode
        if tester.test_connection():
            interactive_mode(tester)
    else:
        # Run full test suite
        tester.run_all_tests()


if __name__ == "__main__":
    main()
