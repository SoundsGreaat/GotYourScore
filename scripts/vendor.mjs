// Copies runtime library dists from node_modules into app/static so the
// app never touches a CDN. Run: npm run vendor (also part of npm run build).
import { cp, mkdir } from "node:fs/promises";
import path from "node:path";

const jobs = [
  // JS runtimes -> served from /static/js/vendor/
  ["node_modules/htmx.org/dist/htmx.min.js", "app/static/js/vendor/htmx.min.js"],
  ["node_modules/quill/dist/quill.js", "app/static/js/vendor/quill.js"],
  ["node_modules/dompurify/dist/purify.min.js", "app/static/js/vendor/purify.min.js"],
  ["node_modules/sortablejs/Sortable.min.js", "app/static/js/vendor/Sortable.min.js"],
  // Quill snow theme CSS -> imported by the Tailwind build (input.css),
  // vendored here so cascade order vs our reskin is explicit.
  ["node_modules/quill/dist/quill.snow.css", "app/static/css/src/vendor/quill.snow.css"],
];

// Inter variable font, normal style. Subsets mirror Google Fonts' default
// unicode-range coverage (latin / latin-ext / cyrillic / cyrillic-ext).
const subsets = ["latin", "latin-ext", "cyrillic", "cyrillic-ext"];
for (const subset of subsets) {
  jobs.push([
    `node_modules/@fontsource-variable/inter/files/inter-${subset}-wght-normal.woff2`,
    `app/static/fonts/inter-${subset}-wght-normal.woff2`,
  ]);
}

await mkdir("app/static/js/vendor", { recursive: true });
await mkdir("app/static/css/src/vendor", { recursive: true });
await mkdir("app/static/fonts", { recursive: true });

for (const [src, dest] of jobs) {
  await cp(path.resolve(src), path.resolve(dest));
  console.log("vendored:", dest);
}
