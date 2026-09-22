/* Scroll behaviour: reveal-on-enter, seamless marquees, and the pinned
   horizontal method section. Nothing decorative. */

(() => {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* --- seamless marquee ---------------------------------------------------
     One track in the markup; repeat its items until a single track is at least
     as wide as the container, then clone the whole track. Two identical tracks
     sit flush and each slides exactly its own width, so as one leaves the other
     is already in place — no gap at the loop point. */
  document.querySelectorAll('.marquee').forEach((m) => {
    const track = m.querySelector('.marquee__track');
    if (!track) return;
    const items = [...track.children];
    let guard = 0;
    while (track.scrollWidth < m.offsetWidth && guard++ < 12) {
      items.forEach((el) => track.appendChild(el.cloneNode(true)));
    }
    const clone = track.cloneNode(true);
    clone.setAttribute('aria-hidden', 'true');
    m.appendChild(clone);
  });

  /* --- reveal on first view ------------------------------------------------ */
  const revealAll = () =>
    document.querySelectorAll('[data-reveal]').forEach((el) => el.classList.add('in'));

  let pending = [];
  if (reduced) {
    revealAll();
  } else {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        e.target.classList.add('in');
        io.unobserve(e.target);
      });
    }, { threshold: 0.1, rootMargin: '0px 0px -5% 0px' });

    pending = [...document.querySelectorAll('[data-reveal]')];
    pending.forEach((el) => io.observe(el));

    // IntersectionObserver delivers nothing while the document is hidden (a
    // background tab, an occluded window), which would leave the page blank
    // until it regains focus. Same geometry test, driven by scroll instead.
    var sweep = () => {
      if (!pending.length) return;
      pending = pending.filter((el) => {
        const r = el.getBoundingClientRect();
        if (r.top < innerHeight * 0.95 && r.bottom > 0) {
          el.classList.add('in');
          io.unobserve(el);
          return false;
        }
        return true;
      });
    };
    sweep();
    addEventListener('load', sweep);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) sweep(); });
  }

  /* --- pinned horizontal section -------------------------------------------
     The section is tall and its inner pin sticks for that whole height, so
     vertical scroll progress through it drives the track sideways. Below 900px
     (and for reduced motion) the panels just stack — see the CSS. */
  const scrollers = [...document.querySelectorAll('[data-hscroll]')].map((section) => ({
    section,
    track: section.querySelector('[data-track]'),
    panels: [...section.querySelectorAll('[data-panel]')],
    steps: [...section.querySelectorAll('[data-stepper] .s')],
    bars: [...section.querySelectorAll('[data-stepper] .bar')],
  })).filter((s) => s.track && s.panels.length);

  const horizontalOn = () => !reduced && matchMedia('(min-width: 900px)').matches;

  // Size each section so one pixel of vertical scroll moves the track one pixel
  // sideways. Without this the section's CSS height (a vh figure) bears no
  // relation to the track's width (a vw figure) and the panels race past.
  // clientWidth, not innerWidth: innerWidth includes the scrollbar.
  const viewW = () => document.documentElement.clientWidth;

  function layoutScrollers() {
    scrollers.forEach(({ section, track }) => {
      if (!horizontalOn()) { section.style.height = ''; return; }
      section.style.height = `${innerHeight + Math.max(0, track.scrollWidth - viewW())}px`;
    });
  }

  function driveScrollers() {
    scrollers.forEach(({ section, track, panels, steps, bars }) => {
      if (!horizontalOn()) {
        track.style.transform = '';
        steps.forEach((s) => s.classList.add('on'));
        bars.forEach((b) => b.classList.add('on'));
        return;
      }
      const travel = section.offsetHeight - innerHeight;
      const p = travel > 0
        ? Math.min(1, Math.max(0, -section.getBoundingClientRect().top / travel))
        : 0;

      track.style.transform = `translate3d(${-(p * (track.scrollWidth - viewW())).toFixed(1)}px,0,0)`;

      const active = Math.min(panels.length - 1, Math.round(p * (panels.length - 1)));
      steps.forEach((el, i) => el.classList.toggle('on', i <= active));
      bars.forEach((el, i) => el.classList.toggle('on', i < active));
    });
  }

  layoutScrollers();
  driveScrollers();
  addEventListener('resize', () => { layoutScrollers(); driveScrollers(); });

  let ticking = false;
  addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      ticking = false;
      driveScrollers();
      if (typeof sweep === 'function') sweep();
    });
  }, { passive: true });
})();
