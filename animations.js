/**
 * animations.js — LuxStay
 * Page fade-in, card reveal, stat counters, parallax, ripple,
 * smooth scroll, lightbox, and promo popup.
 * Depends on: api.js (for showBanner in popup)
 */

// ── PAGE FADE-IN ──────────────────────────────────────────────
document.documentElement.style.cssText += 'opacity:0;transition:opacity .3s ease';
window.addEventListener('load', () => { document.documentElement.style.opacity = '1'; });
if (document.readyState === 'complete') document.documentElement.style.opacity = '1';

// Inject keyframes
const _kf = document.createElement('style');
_kf.textContent = `@keyframes fadeSlideUp{from{opacity:0;transform:translateY(18px)}to{opacity:1;transform:translateY(0)}}@keyframes rippleKF{to{transform:scale(2.8);opacity:0}}`;
document.head.appendChild(_kf);

// ── CARD REVEAL ───────────────────────────────────────────────
let _cardObserver = null;
function observeCards() {
  if (!('IntersectionObserver' in window)) {
    document.querySelectorAll('.hotel-card,.city-card').forEach(c => c.classList.add('revealed'));
    return;
  }
  if (!_cardObserver) {
    _cardObserver = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        const el = entry.target, idx = parseInt(el.dataset.animIndex || 0);
        setTimeout(() => el.classList.add('revealed'), idx * 75);
        _cardObserver.unobserve(el);
      });
    }, { threshold: 0.08, rootMargin: '0px 0px -30px 0px' });
  }
  document.querySelectorAll('.hotel-card,.city-card').forEach((card, idx) => {
    if (card.classList.contains('revealed')) return;
    card.dataset.animIndex = idx;
    _cardObserver.observe(card);
  });
}
window._observeCards = observeCards;
document.addEventListener('DOMContentLoaded', observeCards);

// ── STAT COUNTERS ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  if (!('IntersectionObserver' in window)) return;
  const io = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (!e.isIntersecting) return;
      const el = e.target, text = el.textContent.trim(), match = text.match(/^[\d,]+/);
      if (!match) return;
      const target = parseInt(match[0].replace(/,/g,''), 10), suffix = text.slice(match[0].length);
      const dur = 1200; let startTs = null;
      (function step(ts) {
        if (!startTs) startTs = ts;
        const prog = Math.min((ts-startTs)/dur, 1);
        el.textContent = Math.round((1-Math.pow(1-prog,3))*target).toLocaleString() + suffix;
        if (prog < 1) requestAnimationFrame(step); else el.textContent = target.toLocaleString() + suffix;
      })(performance.now());
      io.unobserve(el);
    });
  }, { threshold: 0.5 });
  document.querySelectorAll('.stat-number').forEach(el => io.observe(el));
});

// ── SECTION HEADING FADE ──────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  if (!('IntersectionObserver' in window)) return;
  const io = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (e.isIntersecting) { e.target.style.animation='fadeSlideUp .55s ease forwards'; io.unobserve(e.target); }
    });
  }, { threshold: 0.15 });
  document.querySelectorAll('h2.section-title,.section-label').forEach(el => { el.style.opacity='0'; io.observe(el); });
});

// ── HERO PARALLAX ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const hero = document.querySelector('.hero'), content = hero?.querySelector('.hero-content');
  if (!hero || !content) return;
  let ticking = false;
  window.addEventListener('scroll', () => {
    if (!ticking) {
      requestAnimationFrame(() => {
        const s = window.scrollY;
        content.style.transform = `translateY(${s*.12}px)`;
        content.style.opacity   = Math.max(0, 1-s/550).toString();
        ticking = false;
      });
      ticking = true;
    }
  }, { passive: true });
});

// ── BUTTON RIPPLE ─────────────────────────────────────────────
document.addEventListener('click', e => {
  const btn = e.target.closest('.btn-primary,.btn-outline'); if (!btn) return;
  const rect = btn.getBoundingClientRect(), size = Math.max(rect.width, rect.height);
  const ripple = document.createElement('span');
  ripple.style.cssText = `position:absolute;width:${size}px;height:${size}px;left:${e.clientX-rect.left-size/2}px;top:${e.clientY-rect.top-size/2}px;background:rgba(255,255,255,.28);border-radius:50%;transform:scale(0);animation:rippleKF .5s ease-out;pointer-events:none`;
  if (getComputedStyle(btn).position === 'static') btn.style.position = 'relative';
  btn.style.overflow = 'hidden';
  btn.appendChild(ripple);
  setTimeout(() => ripple.remove(), 520);
});

// ── SMOOTH ANCHOR SCROLL ──────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('a[href^="#"]').forEach(a => {
    a.addEventListener('click', e => {
      const t = document.querySelector(a.getAttribute('href'));
      if (t) { e.preventDefault(); t.scrollIntoView({ behavior: 'smooth' }); }
    });
  });
});

// ── LIGHTBOX ──────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const lb = document.getElementById('lightbox'), lbImg = document.getElementById('lbImg'), lbCap = document.getElementById('lbCaption');
  if (!lb || !lbImg) return;
  let idx = 0;
  const getImgs = () => window._galleryImages || [];

  function showLb(i) {
    const imgs = getImgs(); if (!imgs.length) return;
    idx = (i + imgs.length) % imgs.length;
    lbImg.src = imgs[idx].image_path || '';
    lbImg.onerror = () => { lbImg.src = 'https://images.unsplash.com/photo-1564501049412-61c2a3083791?w=1200&q=80'; };
    if (lbCap) lbCap.textContent = imgs[idx].caption || '';
  }
  function closeLb() { lb.classList.remove('open'); document.body.style.overflow = ''; }

  window.lbOpen = (i) => { showLb(i); lb.classList.add('open'); document.body.style.overflow = 'hidden'; lb.querySelector('.lb-close')?.focus(); };
  lb.querySelector('.lb-close')?.addEventListener('click', closeLb);
  lb.querySelector('.lb-prev')?.addEventListener('click',  () => showLb(idx-1));
  lb.querySelector('.lb-next')?.addEventListener('click',  () => showLb(idx+1));
  lb.addEventListener('click', e => { if (e.target === lb) closeLb(); });
  document.addEventListener('keydown', e => {
    if (!lb.classList.contains('open')) return;
    if (e.key === 'Escape')     closeLb();
    if (e.key === 'ArrowLeft')  showLb(idx-1);
    if (e.key === 'ArrowRight') showLb(idx+1);
  });
});

// ── PROMO POPUP ───────────────────────────────────────────────
(function () {
  const overlay = document.getElementById('promoOverlay'); 
  if (!overlay) return;

  // Function to open the popup
  function openPopup() { 
    overlay.classList.add('show'); 
    document.body.style.overflow = 'hidden'; 
    setTimeout(() => overlay.querySelector('.popup-cta')?.focus(), 350); 
  }

  // Function to close the popup
  function closePopup() { 
    overlay.classList.remove('show'); 
    document.body.style.overflow = ''; 
    // markDismissed() and its associated logic have been removed to ensure it repeats
  }

  // Only run on the homepage
  if (!document.body.classList.contains('page-home')) return;

  // Show the popup after 3 seconds if the user is not logged in
  setTimeout(() => {
  const isLoggedIn = document.getElementById('navAuth')?.querySelector('.user-avatar-btn');

  // You can use this later if needed
  openPopup();
}, 3000);

  // Event Listeners for closing the popup
  document.getElementById('promoClose')?.addEventListener('click', closePopup);
  document.getElementById('promoSkip')?.addEventListener('click', closePopup);
  
  overlay.addEventListener('click', e => { 
    if (e.target === overlay) closePopup(); 
  });

  document.addEventListener('keydown', e => { 
    if (e.key === 'Escape' && overlay.classList.contains('show')) closePopup(); 
  });
}());