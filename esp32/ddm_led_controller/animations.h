// animations.h - Animation functions for DDM Cup Project

#ifndef ANIMATIONS_H
#define ANIMATIONS_H

#include <FastLED.h>
#include "config.h"
#include "led_control.h"

// Animation state
enum AnimationState {
    ANIM_NONE,
    ANIM_IDLE,
    ANIM_WELCOME,
    ANIM_BETTING_60,
    ANIM_BETTING_30,
    ANIM_FINAL_CALL,
    ANIM_RACE_START,
    ANIM_CHAOS,
    ANIM_FINISH,
    ANIM_HEARTBEAT,
    ANIM_RESULTS
};

extern AnimationState currentAnimation;
extern unsigned long animationStartTime;

// Results tracking
struct ResultsData {
    uint8_t winCup;
    uint8_t placeCup;
    uint8_t showCup;
    bool isActive;
};
extern ResultsData results;

// Animation control functions
void startAnimation(AnimationState anim);
void stopAnimation();
void updateAnimation();
String getAnimationName(AnimationState anim);

// Individual animation functions
void animIdle();
void animWelcome();
void animBetting60();
void animBetting30();
void animFinalCall();
void animRaceStart();
void animChaos();
void animFinish();
void animHeartbeat();
void animResults();

// Utility animation functions
void breathingEffect(CRGB color, float speed);
void pulseEffect(CRGB color, int pulseSpeed);
void strobeEffect(CRGB color, int interval);
void cascadeEffect(const CRGB colors[], int numColors, int delayMs);
void checkeredPattern();

#endif // ANIMATIONS_H
