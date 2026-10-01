import Alpine from 'alpinejs';
import { marked } from 'marked';
import Prism from 'prismjs';
import 'prismjs/themes/prism-tomorrow.css';
import 'prismjs/components/prism-python';
import 'prismjs/components/prism-javascript';
import 'prismjs/components/prism-bash';
import 'prismjs/components/prism-json';

import '@fontsource/inter/400.css';
import '@fontsource/inter/500.css';
import '@fontsource/inter/600.css';
import '@fontsource/inter/700.css';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';
import '@fontsource/jetbrains-mono/600.css';

import { athenaApp } from './app.js';
import './motion-helpers.js';

// Order matters: main.css (Tailwind's generated output) must land before
// the rest so same-specificity ties (e.g. a custom class and a Tailwind
// utility both setting border-radius on the same element) resolve the same
// way the pre-Vite app's cascade did -- see the comment in main.css itself.
import './styles/main.css';
import './styles/tokens.css';
import './styles/base.css';
import './styles/design-system/surfaces.css';
import './styles/design-system/motion.css';

// app.js/index.html were written assuming these are ambient globals (the
// pre-migration app loaded Alpine/marked/Prism as classic <script> tags) --
// rather than touch 3036 lines of working application logic to add import
// statements throughout, the bundled libraries are attached to `window`
// here, once, so every existing `marked.parse(...)`/`Prism.highlightAll()`/
// `x-data="athenaApp()"` reference keeps working completely unchanged.
window.Alpine = Alpine;
window.marked = marked;
window.Prism = Prism;
window.athenaApp = athenaApp;

Alpine.start();
