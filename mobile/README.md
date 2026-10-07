# Fleet Foot phone apps

Two store apps that open the live site: **Fleet Foot Driver** (`/driver`) and **Fleet Foot Kitchen** (`/restaurant`).
Every change pushed to GitHub shows up in both apps right away, no store update needed.

## Store icons
- `driver/assets/icon-only.png`, `kitchen/assets/icon-only.png`: 1024 x 1024 App Store icons
- `*/assets/play-store-512.png`: 512 x 512 Google Play icons
- `icon-foreground.png` / `icon-background.png`: Android adaptive icon layers
- `splash.png`: launch screen

## Build
GitHub > Actions > **Build phone apps (driver + kitchen)** > Run workflow, and enter the live site address.
You get: Android test APKs (install on any Android phone), Android store bundles (AAB, for Google Play), and the iPhone Xcode projects.

## Publish
1. Google Play Console ($25 one time): create two apps, upload the AAB, the 512 icon, screenshots, privacy policy link.
   Sign the AAB with your upload key (Play App Signing).
2. Apple Developer Program ($99 a year): create two apps in App Store Connect with bundle IDs
   `com.fleetfootdelivery.driver` and `com.fleetfootdelivery.kitchen`, open the Xcode zip on a Mac,
   pick your team, Product > Archive > Distribute.
