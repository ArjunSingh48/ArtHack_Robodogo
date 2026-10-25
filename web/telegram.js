// A local illustration of the encounter, not a Telegram client or delivery receipt.
// The show owns timing, pause, and cancellation; this view only reveals its messages.
const previews = new WeakMap();

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function localImage(value) {
  if (!value) return null;
  if (/^data:image\/(?:jpeg|png|webp);base64,[a-z\d+/=\s]+$/i.test(value)) return value;
  try {
    const url = new URL(value, document.baseURI);
    return url.origin === location.origin && ['http:', 'https:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

function sourceLink(value) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

export function createTelegramPreview(container, { photo, photoLabel, artifact, caption } = {}) {
  previews.get(container)?.();
  let disposed = false;
  let frame = null;
  const images = [];
  const shown = new Set();
  const root = element('section', 'telegram-chat');
  root.setAttribute('aria-label', 'Good Boy — Telegram preview');
  const status = element('div', 'telegram-status-bar');
  status.setAttribute('aria-label', 'Phone status bar mockup');
  const clock = element('time', 'telegram-clock', new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date()));
  const icons = element('span', 'telegram-status-icons');
  icons.setAttribute('aria-hidden', 'true');
  // Decorative phone chrome, not readings of the visitor's network or battery.
  icons.innerHTML = `<svg class="telegram-signal" viewBox="0 0 20 16" fill="currentColor"><rect x="1" y="11" width="3" height="4" rx=".7"/><rect x="6" y="8" width="3" height="7" rx=".7"/><rect x="11" y="4" width="3" height="11" rx=".7"/><rect x="16" y="1" width="3" height="14" rx=".7"/></svg>
    <svg class="telegram-wifi" viewBox="0 0 20 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2 5a12 12 0 0 1 16 0M5 8a7.5 7.5 0 0 1 10 0M8 11a3 3 0 0 1 4 0"/><circle cx="10" cy="14" r="1" fill="currentColor" stroke="none"/></svg>
    <svg class="telegram-battery" viewBox="0 0 28 16" fill="none"><rect x="1" y="2" width="22" height="12" rx="3" stroke="currentColor" stroke-width="1.4"/><rect x="3.5" y="4.5" width="15" height="7" rx="1" fill="currentColor"/><path d="M25 6v4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;
  status.append(clock, icons);
  const header = element('header', 'telegram-header');
  const avatar = element('img', 'telegram-avatar');
  avatar.src = 'img/good-boy-avatar.jpg';
  avatar.alt = 'Good Boy, the robot dog';
  const identity = element('div', 'telegram-identity');
  identity.append(element('h2', '', 'Good Boy'), element('span', 'telegram-preview-label', 'Telegram preview'));
  header.append(avatar, identity);
  const messages = element('div', 'telegram-messages');
  messages.setAttribute('role', 'log');
  messages.setAttribute('aria-live', 'polite');
  messages.setAttribute('aria-relevant', 'additions');
  const home = element('div', 'telegram-home-indicator');
  home.setAttribute('aria-hidden', 'true');
  root.append(status, header, messages, home);
  container.replaceChildren(root);
  document.body.classList.add('telegram-preview');

  function scrollToLatest() {
    if (disposed) return;
    // Do not depend on an animation frame: background tabs may throttle it.
    // Adjust again next frame after the browser has composed the arriving bubble.
    messages.scrollTop = messages.scrollHeight;
    if (frame !== null) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      frame = null;
      if (!disposed) messages.scrollTop = messages.scrollHeight;
    });
  }

  function imageBubble(kind, src, alt) {
    const bubble = element('article', `telegram-message telegram-${kind}`);
    bubble.setAttribute('aria-label', `${kind === 'photo' ? 'Photo' : 'Painting'} from Good Boy`);
    const url = localImage(src);
    if (url) {
      const img = element('img', 'telegram-image');
      img.alt = alt;
      img.decoding = 'async';
      img.onload = scrollToLatest;
      img.onerror = () => {
        if (disposed) return;
        img.replaceWith(element('p', 'telegram-image-unavailable', `${kind === 'photo' ? 'Photo' : 'Painting'} unavailable`));
        scrollToLatest();
      };
      img.src = url;
      images.push(img);
      bubble.append(img);
    } else bubble.append(element('p', 'telegram-image-unavailable', `${kind === 'photo' ? 'Photo' : 'Painting'} unavailable`));
    return bubble;
  }

  function appendOnce(kind, build) {
    if (disposed || shown.has(kind)) return;
    shown.add(kind);
    messages.append(build());
    scrollToLatest();
  }

  function showPhoto() {
    if (!photo) return;
    appendOnce('photo', () => {
      const bubble = imageBubble('photo', photo, photoLabel || 'The encounter photograph');
      if (photoLabel) bubble.append(element('p', 'telegram-photo-label', photoLabel));
      return bubble;
    });
  }

  function showPainting() {
    if (!artifact || disposed) return;
    root.classList.add('telegram-chat--has-art');
    appendOnce('painting', () => {
      const bubble = imageBubble('painting', artifact.image, artifact.title || 'The selected painting');
      const body = element('div', 'telegram-message-body');
      if (caption) body.append(element('p', 'telegram-caption', caption));
      const credit = element('i', 'telegram-credit');
      const url = sourceLink(artifact.source_url || artifact.page_url);
      if (artifact.attribution) {
        credit.append(document.createTextNode(artifact.attribution));
      } else {
        const title = [artifact.artist, artifact.title].filter(Boolean).join(' — ');
        const rights = [...new Set([artifact.rights, artifact.rights_statement, artifact.rights_credit,
          artifact.source_statement, artifact.credit].filter(Boolean))].join('. ');
        credit.append(document.createTextNode([
          `${title}${artifact.date ? ', ' + artifact.date : ''}`,
          artifact.museum, rights,
        ].filter(Boolean).join('. ')));
      }
      if (url) {
        credit.append(document.createTextNode(' '));
        const link = element('a', '', 'Source');
        link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
        credit.append(link);
      }
      body.append(credit);
      bubble.append(body);
      return bubble;
    });
  }

  function showTyping() {
    appendOnce('typing', () => {
      const bubble = element('div', 'telegram-message telegram-typing');
      bubble.setAttribute('aria-label', 'Good Boy is typing');
      for (let i = 0; i < 3; i++) {
        const dot = element('span'); dot.setAttribute('aria-hidden', 'true'); bubble.append(dot);
      }
      return bubble;
    });
  }

  function showQuestion(text) {
    if (disposed || !text) return;
    messages.querySelector('.telegram-typing')?.remove();
    appendOnce('question', () => element('p', 'telegram-message telegram-question', text));
  }

  function setPaused(paused) {
    if (!disposed) root.classList.toggle('telegram-chat--paused', Boolean(paused));
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    if (frame !== null) cancelAnimationFrame(frame);
    for (const img of images) { img.onload = null; img.onerror = null; img.removeAttribute('src'); }
    images.length = 0;
    photo = null; artifact = null;
    root.replaceChildren(); root.remove();
    if (previews.get(container) === dispose) {
      previews.delete(container);
      document.body.classList.remove('telegram-preview');
    }
  }
  previews.set(container, dispose);
  return { showPhoto, showPainting, showTyping, showQuestion, setPaused, dispose };
}
