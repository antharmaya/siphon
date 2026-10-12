// Build-time distribution policy. This source copy is the GitHub build: no restrictions.
//
// scripts/build-extension.py rewrites this file for the Chrome Web Store build, where it lists
// the hosts that build must not download from. background.js loads it and refuses those hosts;
// the store manifest also keeps the content script (the on-video button) off them.
self.SIPHON_POLICY = {
  edition: "github",
  blockedHosts: [],
  blockedMessage: "",
  hostHelp: "",  // empty: background.js says "run host/install.sh", which ships in this build
};
