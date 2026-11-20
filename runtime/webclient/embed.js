// Studio owns navigation, identity and framing; enforce relay-only ICE in this viewport.
document.documentElement.dataset.embedded = 'true';
const style = document.createElement('style');
style.textContent = '#control-panel-app,#control-panel-loader,.fab-container,.v-navigation-drawer{display:none!important}html,body{margin:0;overflow:hidden!important}';
document.head.append(style);
document.addEventListener('pointerdown', event => {
  if (event.target instanceof Element && event.target.closest('#stream')) {
    setTimeout(() => {
      window.focus();
      document.body.tabIndex = -1;
      document.body.focus({ preventScroll: true });
    }, 0);
  }
}, true);
const NativePeer = window.RTCPeerConnection;
window.RTCPeerConnection = class extends NativePeer {
  constructor(config, ...rest) {
    super({ ...config, iceTransportPolicy: 'relay' }, ...rest);
  }
  setConfiguration(config) {
    return super.setConfiguration({ ...config, iceTransportPolicy: 'relay' });
  }
};
