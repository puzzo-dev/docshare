// Bundle entrypoint for DocShare desk assets.
//
// This must be a *.bundle.js file: esbuild globs public/**/*.bundle.{js,css}
// and emits the output with a content hash in the filename
// (dist/js/docshare.bundle.<HASH>.js), which assets.json maps back to
// "docshare.bundle.js". Referencing a raw path like
// /assets/docshare/js/share_menu.js instead would ship an unhashed URL, and
// include_script() adds no cache-busting query string — so browsers would
// keep serving a stale copy after every rebuild.

import "./share_menu.js";
