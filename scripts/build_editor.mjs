import {build} from "esbuild";
import {mkdir, copyFile} from "node:fs/promises";

const output = "static/editor";
await mkdir(output, {recursive: true});
await build({
  entryPoints: ["frontend/editor.js"], outfile: `${output}/editor.js`,
  bundle: true, minify: true, format: "iife", globalName: "ContestEditor",
  target: "es2020", loader: {".ttf": "file"}, assetNames: "[name]-[hash]",
  legalComments: "linked",
});
await build({
  entryPoints: ["node_modules/monaco-editor/esm/vs/editor/editor.worker.js"],
  outfile: `${output}/editor.worker.js`, bundle: true, minify: true,
  format: "iife", target: "es2020", legalComments: "linked",
});
for (const name of ["LICENSE", "ThirdPartyNotices.txt"]) {
  await copyFile(`node_modules/monaco-editor/${name}`, `${output}/${name}`);
}
console.log("Built local Monaco editor, C++ support, styles and worker.");
