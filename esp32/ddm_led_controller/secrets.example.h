// secrets.example.h - the LED controller's Wi-Fi network, the shape of secrets.h.
//
// Copy this file to secrets.h beside ddm_led_controller.ino and fill in the
// network the mantle's controller joins. secrets.h is git-ignored: the repo is
// public, so the real values never go in a tracked file. The sketch includes
// secrets.h, so it won't build until the file exists.

#ifndef DDM_LED_SECRETS_H
#define DDM_LED_SECRETS_H

#define WIFI_SSID "YOUR_WIFI_SSID"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"

#endif
