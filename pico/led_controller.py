# led_controller.py - WS2812B LED Strip Controller for DDM Cup Project

from machine import Pin
import neopixel
import time
from config import LED_PIN, LED_COUNT, LEDS_PER_CUP, NUM_CUPS, DEFAULT_BRIGHTNESS, MAX_BRIGHTNESS, DDM_COLORS


class LEDController:
    """Controls WS2812B LED strip with 320 LEDs (16 per cup × 20 cups)"""
    
    def __init__(self):
        """Initialize the LED controller"""
        self.pin = Pin(LED_PIN, Pin.OUT)
        self.strip = neopixel.NeoPixel(self.pin, LED_COUNT)
        self.brightness = DEFAULT_BRIGHTNESS
        self._current_state = [(0, 0, 0)] * LED_COUNT  # Track current colors
        print(f"[LED] Initialized {LED_COUNT} LEDs on GPIO {LED_PIN}")
    
    def set_brightness(self, percent):
        """
        Set global brightness (0-100%)
        
        Args:
            percent: Brightness percentage (0-100)
        """
        self.brightness = max(0, min(MAX_BRIGHTNESS, percent))
        print(f"[LED] Brightness set to {self.brightness}%")
    
    def _apply_brightness(self, color):
        """
        Apply current brightness to a color tuple
        
        Args:
            color: RGB tuple (0-255, 0-255, 0-255)
        
        Returns:
            Brightness-adjusted RGB tuple
        """
        r, g, b = color
        factor = self.brightness / 100.0
        return (int(r * factor), int(g * factor), int(b * factor))
    
    def set_all(self, color):
        """
        Set all LEDs to the same color
        
        Args:
            color: RGB tuple (0-255, 0-255, 0-255) or color name from DDM_COLORS
        """
        if isinstance(color, str):
            color = DDM_COLORS.get(color.lower(), (0, 0, 0))
        
        adjusted_color = self._apply_brightness(color)
        
        for i in range(LED_COUNT):
            self.strip[i] = adjusted_color
            self._current_state[i] = color
        
        self.strip.write()
        print(f"[LED] All LEDs set to {color}")
    
    def set_cup(self, cup_number, color):
        """
        Set a specific cup to a color (cups numbered 1-20)
        
        Args:
            cup_number: Cup number (1-20)
            color: RGB tuple (0-255, 0-255, 0-255) or color name from DDM_COLORS
        """
        if not 1 <= cup_number <= NUM_CUPS:
            print(f"[LED] Error: Cup number {cup_number} out of range (1-{NUM_CUPS})")
            return
        
        if isinstance(color, str):
            color = DDM_COLORS.get(color.lower(), (0, 0, 0))
        
        adjusted_color = self._apply_brightness(color)
        
        # Calculate LED indices for this cup (0-indexed internally)
        start_idx = (cup_number - 1) * LEDS_PER_CUP
        end_idx = start_idx + LEDS_PER_CUP
        
        for i in range(start_idx, end_idx):
            self.strip[i] = adjusted_color
            self._current_state[i] = color
        
        self.strip.write()
        print(f"[LED] Cup {cup_number} set to {color}")
    
    def set_pixel(self, index, color):
        """
        Set a specific LED pixel by index (0-319)
        
        Args:
            index: LED index (0-319)
            color: RGB tuple (0-255, 0-255, 0-255)
        """
        if not 0 <= index < LED_COUNT:
            print(f"[LED] Error: Pixel index {index} out of range (0-{LED_COUNT-1})")
            return
        
        adjusted_color = self._apply_brightness(color)
        self.strip[index] = adjusted_color
        self._current_state[index] = color
    
    def update(self):
        """Write the current buffer to the LED strip"""
        self.strip.write()
    
    def all_on(self):
        """Turn all LEDs on (white)"""
        self.set_all((255, 255, 255))
        print("[LED] All LEDs ON (white)")
    
    def all_off(self):
        """Turn all LEDs off (black)"""
        self.set_all((0, 0, 0))
        print("[LED] All LEDs OFF")
    
    def clear(self):
        """Clear all LEDs (same as all_off)"""
        self.all_off()
    
    def test_sequence(self):
        """
        Run a test sequence - light each cup sequentially
        Useful for verifying wiring and numbering
        """
        print("[LED] Starting test sequence...")
        
        # Clear all first
        self.all_off()
        time.sleep(0.5)
        
        # Light each cup one at a time
        test_colors = ["green", "gold", "rose", "white"]
        
        for cup in range(1, NUM_CUPS + 1):
            color = test_colors[(cup - 1) % len(test_colors)]
            self.set_cup(cup, color)
            time.sleep(0.2)
        
        time.sleep(1)
        self.all_off()
        print("[LED] Test sequence complete")
    
    def hex_to_rgb(self, hex_color):
        """
        Convert hex color string to RGB tuple
        
        Args:
            hex_color: Hex color string (e.g., "FFD700" or "#FFD700")
        
        Returns:
            RGB tuple (0-255, 0-255, 0-255)
        """
        hex_color = hex_color.lstrip('#')
        
        if len(hex_color) != 6:
            print(f"[LED] Error: Invalid hex color '{hex_color}'")
            return (0, 0, 0)
        
        try:
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            return (r, g, b)
        except ValueError:
            print(f"[LED] Error: Could not parse hex color '{hex_color}'")
            return (0, 0, 0)
    
    def get_cup_range(self, cup_number):
        """
        Get the LED index range for a specific cup
        
        Args:
            cup_number: Cup number (1-20)
        
        Returns:
            Tuple of (start_index, end_index)
        """
        if not 1 <= cup_number <= NUM_CUPS:
            return None
        
        start_idx = (cup_number - 1) * LEDS_PER_CUP
        end_idx = start_idx + LEDS_PER_CUP
        return (start_idx, end_idx)
    
    def get_status(self):
        """
        Get current LED controller status
        
        Returns:
            Dict with status information
        """
        return {
            "total_leds": LED_COUNT,
            "cups": NUM_CUPS,
            "leds_per_cup": LEDS_PER_CUP,
            "brightness": self.brightness,
            "pin": LED_PIN
        }
