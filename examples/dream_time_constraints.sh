#!/bin/bash
# Example: Configure Dream Auto time constraints for night-only operation
# Usage: source this file or copy env vars into your ~/.bashrc / ~/.zshrc

# ============================================================================
# Dream Auto Time Window Configuration
# ============================================================================

# Allow dreams ONLY between 10 PM and 6 AM local time
export DREAM_AUTO_ALLOW_HOURS="22:00-06:00"

# Set your timezone (omit for UTC)
# Common values: America/New_York, America/Los_Angeles, Europe/London, Asia/Tokyo
export DREAM_AUTO_TIMEZONE="America/New_York"

# Cap to 5 dreams per calendar day
export DREAM_AUTO_MAX_DAILY_DREAMS=5

# ============================================================================
# Alternative: Block work hours instead of specifying allow window
# ============================================================================
# export DREAM_AUTO_DENY_HOURS="09:00-18:00"

# ============================================================================
# Force Override (one-time use)
# ============================================================================
# To manually trigger a dream right now, bypassing all time/cap constraints:
#
#   export DREAM_AUTO_FORCE_ALLOW_NEXT_RUN=1
#   hermes cron run dream-scheduler
#
# The flag is automatically cleared after the first use.

# ============================================================================
# Kubernetes / Container Deployment
# ============================================================================
# Add to your Hermes deployment:
#
#   env:
#     - name: DREAM_AUTO_ALLOW_HOURS
#       value: "22:00-06:00"
#     - name: DREAM_AUTO_TIMEZONE
#       value: "America/New_York"
#     - name: DREAM_AUTO_MAX_DAILY_DREAMS
#       value: "5"
#
# Then restart the Hermes gateway:
#   kubectl rollout restart deployment/hermes-gateway -n hermes

echo "Dream Auto time constraints configured:"
echo "  Allow: $DREAM_AUTO_ALLOW_HOURS (tz: $DREAM_AUTO_TIMEZONE)"
echo "  Daily cap: $DREAM_AUTO_MAX_DAILY_DREAMS"
