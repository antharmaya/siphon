// Build-time distribution policy. This source copy is the GitHub build: no restrictions.
//
// scripts/build-extension.py rewrites this file per edition. The Chrome Web Store build lists
// the hosts it must not download from; the Firefox build only changes the help text. background.js loads it and refuses those hosts;
// the store manifest also keeps the content script (the on-video button) off them.
self.SIPHON_POLICY = {
  edition: "github",
  blockedHosts: [],
  blockedMessage: "",
  hostHelp: "",  // empty: background.js says "run: siphon setup browser"
};
