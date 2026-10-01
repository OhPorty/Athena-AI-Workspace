import { animate, stagger } from 'motion';

// Attached to window rather than imported directly by app.js -- keeps the
// 3036-line existing Alpine component architecturally separate from the
// animation library, so app.js never needs to know `motion` exists at all.
// Chat-page markup calls these directly: x-init="athenaMotion.revealMessage($el)".
const EASE_LIQUID = [0.34, 1.56, 0.64, 1];

// Spring physics passed straight to animate() -- `motion`'s standalone
// spring() generator (a different, lower-level API in this package version)
// expects pre-resolved keyframes and throws if called the "give me an easing
// curve" way older Framer Motion docs describe. animate() itself accepts
// type: 'spring' + stiffness/damping directly, so that's used here instead.
const SPRING = { type: 'spring', stiffness: 300, damping: 22 };

const athenaMotion = {
    /**
     * Fired once per message DOM node via x-init inside the x-for loop.
     * Alpine keys x-for by index and mutates the same node in place as a
     * message streams in, so x-init fires exactly once per node's
     * creation -- this can't re-trigger mid-stream, no guard needed here.
     */
    revealMessage(el) {
        if (!el || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
        animate(el, { opacity: [0, 1], transform: ['translateY(8px)', 'translateY(0)'] }, SPRING);
    },

    /**
     * Staggers a set of already-in-DOM elements (e.g. a freshly-rendered
     * batch of message children) -- separate from revealMessage since most
     * messages arrive one at a time via streaming, not in a batch.
     */
    revealStagger(elements) {
        if (!elements || !elements.length || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
        animate(elements, { opacity: [0, 1], transform: ['translateY(8px)', 'translateY(0)'] }, { ...SPRING, delay: stagger(0.06) });
    },

    /**
     * Soft squash-and-stretch press feedback for the composer send button --
     * called from @click alongside (not instead of) the existing send
     * handler, purely additive.
     */
    pressSquash(el) {
        if (!el || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
        animate(el, { transform: ['scale(1)', 'scale(0.88)', 'scale(1)'] }, {
            duration: 0.35,
            easing: EASE_LIQUID,
        });
    },
};

window.athenaMotion = athenaMotion;
export default athenaMotion;
