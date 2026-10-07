import type { CapacitorConfig } from '@capacitor/cli';

// The app opens your live site. Set SITE_URL when building, for example
// SITE_URL=https://your-app.up.railway.app npx cap sync
const site = (process.env.SITE_URL || 'https://YOUR-SITE.up.railway.app').replace(/\/$/, '');

const config: CapacitorConfig = {
  appId: 'com.fleetfootdelivery.kitchen',
  appName: 'Fleet Foot Kitchen',
  webDir: 'www',
  server: {
    url: site + '/restaurant',
    cleartext: false,
    allowNavigation: [new URL(site).host, '*.up.railway.app', '*.paypal.com', '*.branchapp.com'],
  },
  android: { backgroundColor: '#ffffff' },
  ios: { backgroundColor: '#ffffff', contentInset: 'always' },
  plugins: { SplashScreen: { launchShowDuration: 800, backgroundColor: '#ffffff' } },
};

export default config;
