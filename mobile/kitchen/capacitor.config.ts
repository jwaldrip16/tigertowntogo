import type { CapacitorConfig } from '@capacitor/cli';

// The app opens your live site. Set SITE_URL when building, for example
// SITE_URL=https://your-app.up.railway.app npx cap sync
const site = (process.env.SITE_URL || 'https://tigertowntogo.up.railway.app').replace(/\/$/, '');

const config: CapacitorConfig = {
  appId: 'com.fleetfootdelivery.kitchen',
  appName: 'Fleet Foot Kitchen',
  webDir: 'www',
  server: {
    url: site + '/go/kitchen',   // company picker first, then that company's sign in
    cleartext: false,
    // every client company has its own web address, so the app may open any of them
    allowNavigation: ['*'],
  },
  android: { backgroundColor: '#ffffff' },
  ios: { backgroundColor: '#ffffff', contentInset: 'always' },
  plugins: { SplashScreen: { launchShowDuration: 800, backgroundColor: '#ffffff' } },
};

export default config;
