# main.py - DDM Cup LED Controller (Pico 2 W)
# Main entry point for the LED controller system

import network
import socket
import time
from machine import Pin
from led_controller import LEDController
from oled_display import OLEDDisplay
from config import (
    WIFI_SSID, WIFI_PASSWORD, SOCKET_PORT, SOCKET_TIMEOUT,
    DDM_COLORS, STATUS_LED_PIN
)

# Global variables
led_controller = None
oled = None
wlan = None
server_socket = None
status_led = None
current_animation = "Idle"
last_command = ""


def init_status_led():
    """Initialize the built-in status LED"""
    global status_led
    try:
        status_led = Pin(STATUS_LED_PIN, Pin.OUT)
        status_led.value(0)
        print("[STATUS] Status LED initialized")
    except Exception as e:
        print(f"[STATUS] Error initializing status LED: {e}")
        status_led = None


def blink_status_led(times=1, delay=0.1):
    """Blink the status LED"""
    if status_led:
        for _ in range(times):
            status_led.value(1)
            time.sleep(delay)
            status_led.value(0)
            time.sleep(delay)


def connect_wifi():
    """Connect to WiFi network"""
    global wlan
    
    print("\n" + "="*50)
    print("DDM Cup LED Controller - Pico 2 W")
    print("="*50)
    
    # Initialize WLAN interface
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    
    # Show connecting message on OLED
    if oled:
        oled.show_connecting(WIFI_SSID)
    
    print(f"[WiFi] Connecting to '{WIFI_SSID}'...")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    
    # Wait for connection with timeout
    max_wait = 20  # 20 seconds timeout
    while max_wait > 0:
        if wlan.status() < 0 or wlan.status() >= 3:
            break
        max_wait -= 1
        print(f"[WiFi] Waiting for connection... ({max_wait}s)")
        blink_status_led(1, 0.1)
        time.sleep(1)
    
    # Check connection status
    if wlan.status() != 3:
        print("[WiFi] ERROR: Failed to connect!")
        if oled:
            oled.show_error("WiFi Failed")
        raise RuntimeError('WiFi connection failed')
    
    # Connection successful
    ip = wlan.ifconfig()[0]
    print(f"[WiFi] Connected successfully!")
    print(f"[WiFi] IP Address: {ip}")
    print(f"[WiFi] Netmask: {wlan.ifconfig()[1]}")
    print(f"[WiFi] Gateway: {wlan.ifconfig()[2]}")
    print("="*50 + "\n")
    
    # Show success on OLED
    if oled:
        oled.show_connected(ip)
    
    # Blink status LED to indicate success
    blink_status_led(3, 0.2)
    
    return ip


def start_socket_server(ip):
    """Start the socket server to listen for commands"""
    global server_socket
    
    addr = socket.getaddrinfo(ip, SOCKET_PORT)[0][-1]
    server_socket = socket.socket()
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(addr)
    server_socket.listen(1)
    server_socket.settimeout(SOCKET_TIMEOUT)  # Non-blocking with timeout
    
    print(f"[Socket] Server listening on {ip}:{SOCKET_PORT}")
    print(f"[Socket] Ready to receive commands from Pi5")
    print("="*50 + "\n")
    
    return server_socket


def parse_command(cmd_str):
    """
    Parse command string and return command parts
    
    Format: COMMAND:ACTION:VALUE
    Examples:
        PING
        LED:ALL_ON
        LED:ALL_OFF
        LED:BRIGHTNESS:75
        LED:COLOR:FFD700
        LED:CUP:5:DC143C
    """
    parts = cmd_str.strip().split(':')
    return parts


def handle_command(cmd_str):
    """Handle incoming command"""
    global current_animation, last_command
    
    print(f"[CMD] Received: '{cmd_str}'")
    last_command = cmd_str[:20]  # Truncate for OLED display
    
    parts = parse_command(cmd_str)
    
    if not parts:
        return "ERROR:EMPTY_COMMAND"
    
    command = parts[0].upper()
    
    try:
        # PING command - connection test
        if command == "PING":
            print("[CMD] PING received, responding with PONG")
            blink_status_led(1, 0.05)
            return "PONG"
        
        # RESET command
        elif command == "RESET":
            print("[CMD] Reset - clearing all LEDs")
            led_controller.all_off()
            current_animation = "Idle"
            return "OK:RESET"
        
        # LED commands
        elif command == "LED":
            if len(parts) < 2:
                return "ERROR:INVALID_LED_COMMAND"
            
            action = parts[1].upper()
            
            # LED:ALL_ON
            if action == "ALL_ON":
                led_controller.all_on()
                current_animation = "All On"
                return "OK:ALL_ON"
            
            # LED:ALL_OFF
            elif action == "ALL_OFF":
                led_controller.all_off()
                current_animation = "All Off"
                return "OK:ALL_OFF"
            
            # LED:BRIGHTNESS:XX
            elif action == "BRIGHTNESS":
                if len(parts) < 3:
                    return "ERROR:MISSING_BRIGHTNESS_VALUE"
                try:
                    brightness = int(parts[2])
                    led_controller.set_brightness(brightness)
                    return f"OK:BRIGHTNESS:{brightness}"
                except ValueError:
                    return "ERROR:INVALID_BRIGHTNESS"
            
            # LED:COLOR:RRGGBB
            elif action == "COLOR":
                if len(parts) < 3:
                    return "ERROR:MISSING_COLOR_VALUE"
                hex_color = parts[2]
                rgb = led_controller.hex_to_rgb(hex_color)
                led_controller.set_all(rgb)
                current_animation = f"Color {hex_color}"
                return f"OK:COLOR:{hex_color}"
            
            # LED:CUP:N:RRGGBB
            elif action == "CUP":
                if len(parts) < 4:
                    return "ERROR:MISSING_CUP_PARAMS"
                try:
                    cup_num = int(parts[2])
                    hex_color = parts[3]
                    rgb = led_controller.hex_to_rgb(hex_color)
                    led_controller.set_cup(cup_num, rgb)
                    return f"OK:CUP:{cup_num}:{hex_color}"
                except ValueError:
                    return "ERROR:INVALID_CUP_NUMBER"
            
            else:
                return f"ERROR:UNKNOWN_LED_ACTION:{action}"
        
        # ANIM commands (placeholder for now)
        elif command == "ANIM":
            if len(parts) < 2:
                return "ERROR:INVALID_ANIM_COMMAND"
            
            anim_name = parts[1].upper()
            current_animation = anim_name
            print(f"[CMD] Animation requested: {anim_name} (not yet implemented)")
            
            # For now, just show a simple effect based on animation name
            if anim_name == "IDLE":
                led_controller.set_all(DDM_COLORS["green"])
            elif anim_name == "WELCOME":
                led_controller.test_sequence()
            else:
                led_controller.set_all(DDM_COLORS["green"])
            
            return f"OK:ANIM:{anim_name}"
        
        else:
            return f"ERROR:UNKNOWN_COMMAND:{command}"
    
    except Exception as e:
        error_msg = f"ERROR:EXCEPTION:{str(e)}"
        print(f"[CMD] Exception handling command: {e}")
        return error_msg


def update_oled_status(ip):
    """Update OLED display with current status"""
    if oled and oled.is_enabled():
        status = "Listening"
        oled.update_status(
            ip=ip,
            status=status,
            animation=current_animation,
            last_cmd=last_command
        )


def main_loop(ip):
    """Main loop - listen for commands and handle them"""
    global server_socket
    
    print("[Main] Entering main loop...")
    print("[Main] Send commands from Pi5 to control LEDs")
    print("[Main] Press Ctrl+C to exit\n")
    
    # Turn on status LED to indicate ready
    if status_led:
        status_led.value(1)
    
    # Update OLED
    update_oled_status(ip)
    
    # Main loop
    connection = None
    last_oled_update = time.time()
    
    while True:
        try:
            # Try to accept a connection (non-blocking with timeout)
            try:
                connection, addr = server_socket.accept()
                connection.settimeout(SOCKET_TIMEOUT)
                print(f"[Socket] Connection from {addr}")
                
                # Receive data
                data = connection.recv(1024)
                if data:
                    cmd_str = data.decode('utf-8').strip()
                    response = handle_command(cmd_str)
                    
                    # Send response
                    connection.send(response.encode('utf-8'))
                    print(f"[CMD] Response: '{response}'")
                    
                    # Update OLED after command
                    update_oled_status(ip)
                
                # Close connection
                connection.close()
                connection = None
                
            except OSError as e:
                # Timeout - no connection, continue loop
                pass
            
            # Periodic OLED update (every 5 seconds)
            if time.time() - last_oled_update > 5:
                update_oled_status(ip)
                last_oled_update = time.time()
            
            # Small delay to prevent tight loop
            time.sleep(0.01)
        
        except KeyboardInterrupt:
            print("\n[Main] Keyboard interrupt - shutting down...")
            break
        
        except Exception as e:
            print(f"[Main] Error in main loop: {e}")
            if connection:
                connection.close()
                connection = None
            time.sleep(1)
    
    # Cleanup
    cleanup()


def cleanup():
    """Cleanup resources before exit"""
    print("\n[Cleanup] Shutting down...")
    
    # Turn off LEDs
    if led_controller:
        led_controller.all_off()
        print("[Cleanup] LEDs off")
    
    # Turn off status LED
    if status_led:
        status_led.value(0)
    
    # Close socket
    if server_socket:
        server_socket.close()
        print("[Cleanup] Socket closed")
    
    # Clear OLED
    if oled:
        oled.show_message("", "Shutdown", "Complete", "")
    
    print("[Cleanup] Done\n")


def main():
    """Main entry point"""
    global led_controller, oled
    
    try:
        # Initialize status LED
        init_status_led()
        
        # Initialize OLED display
        print("[Init] Initializing OLED display...")
        oled = OLEDDisplay()
        
        # Initialize LED controller
        print("[Init] Initializing LED controller...")
        led_controller = LEDController()
        led_controller.all_off()  # Start with LEDs off
        
        # Connect to WiFi
        ip = connect_wifi()
        
        # Start socket server
        start_socket_server(ip)
        
        # Enter main loop
        main_loop(ip)
    
    except Exception as e:
        print(f"\n[FATAL] Error: {e}")
        if oled:
            oled.show_error(str(e)[:32])
        # Blink error pattern
        for _ in range(5):
            blink_status_led(3, 0.1)
            time.sleep(0.5)
    
    finally:
        cleanup()


# Run main function
if __name__ == "__main__":
    main()
