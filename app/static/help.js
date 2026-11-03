'use strict';

let helpAnchor = null, helpBubble = null, helpCloseTimer, tappedHelpToClose = null;

function help(text, label = 'Подробнее') {
  return `<button type="button" class="help-trigger" data-help="${escapeHTML(text)}" aria-label="${escapeHTML(label)}">?</button>`;
}

function hideHelp() {
  clearTimeout(helpCloseTimer);
  if (helpAnchor) {
    const ids = (helpAnchor.getAttribute('aria-describedby') || '').split(' ').filter(id => id && id !== 'ui-tooltip');
    if (ids.length) helpAnchor.setAttribute('aria-describedby', ids.join(' '));
    else helpAnchor.removeAttribute('aria-describedby');
  }
  helpAnchor = null;
  if (helpBubble) helpBubble.hidden = true;
}

function positionHelp() {
  if (!helpAnchor?.isConnected) return hideHelp();
  const rect = helpAnchor.getBoundingClientRect();
  const bubble = helpBubble.getBoundingClientRect();
  const left = Math.max(12, Math.min(rect.left, innerWidth - bubble.width - 12));
  const above = rect.top - bubble.height - 8;
  const top = above >= 12 ? above : Math.max(12, Math.min(rect.bottom + 8, innerHeight - bubble.height - 12));
  helpBubble.style.left = left + 'px';
  helpBubble.style.top = top + 'px';
}

function showHelp(anchor) {
  if (!anchor?.dataset.help) return;
  clearTimeout(helpCloseTimer);
  if (helpAnchor !== anchor) hideHelp();
  if (!helpBubble) {
    helpBubble = document.createElement('div');
    helpBubble.id = 'ui-tooltip';
    helpBubble.className = 'help-tooltip';
    helpBubble.setAttribute('role', 'tooltip');
    helpBubble.addEventListener('pointerenter', () => clearTimeout(helpCloseTimer));
    helpBubble.addEventListener('pointerleave', () => { helpCloseTimer = setTimeout(hideHelp, 120); });
  }
  // A dialog belongs to the browser's top layer; its tooltip must live there too.
  const parent = anchor.closest('dialog[open]') || document.body;
  if (helpBubble.parentNode !== parent) parent.append(helpBubble);
  helpAnchor = anchor;
  helpBubble.textContent = anchor.dataset.help;
  helpBubble.hidden = false;
  const ids = new Set((anchor.getAttribute('aria-describedby') || '').split(' ').filter(Boolean));
  ids.add('ui-tooltip');
  anchor.setAttribute('aria-describedby', [...ids].join(' '));
  positionHelp();
}

function setHelp(selector, text) {
  const node = document.querySelector(selector);
  if (!node) return;
  node.dataset.help = text;
  if (helpAnchor === node) showHelp(node);
}

document.addEventListener('pointerover', event => {
  if (event.pointerType === 'touch') return;
  const anchor = event.target.closest('[data-help]');
  if (anchor) showHelp(anchor);
});
document.addEventListener('pointerout', event => {
  if (event.pointerType === 'touch') return;
  if (!helpAnchor || helpAnchor.contains(event.relatedTarget) || helpBubble?.contains(event.relatedTarget)) return;
  helpCloseTimer = setTimeout(hideHelp, 120);
});
document.addEventListener('focusin', event => {
  const anchor = event.target.closest('[data-help]');
  if (anchor) showHelp(anchor);
  else hideHelp();
});
document.addEventListener('focusout', event => {
  if (event.target === helpAnchor) hideHelp();
});
document.addEventListener('pointerdown', event => {
  const anchor = event.target.closest('.help-trigger');
  tappedHelpToClose = event.pointerType === 'touch' && anchor === helpAnchor ? anchor : null;
});
document.addEventListener('click', event => {
  const anchor = event.target.closest('.help-trigger');
  if (anchor && tappedHelpToClose === anchor) hideHelp();
  else if (anchor) showHelp(anchor);
  else if (!helpBubble?.contains(event.target)) hideHelp();
  tappedHelpToClose = null;
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && helpAnchor) {
    hideHelp();
    event.preventDefault();
    event.stopPropagation();
  }
}, true);
document.addEventListener('scroll', hideHelp, true);
window.addEventListener('resize', hideHelp);
document.getElementById('modal').addEventListener('close', hideHelp);
