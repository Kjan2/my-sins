import * as monaco from "monaco-editor/esm/vs/editor/editor.api.js";
import "monaco-editor/esm/vs/editor/editor.all.js";
import "monaco-editor/esm/vs/basic-languages/cpp/cpp.contribution.js";

// All assets and the worker are served by Django/staticfiles on the same origin.
const assetBase = new URL(".", document.currentScript.src);
self.MonacoEnvironment = {
  getWorker() { return new Worker(new URL("editor.worker.js", assetBase)); },
};
monaco.editor.defineTheme("department", {
  base: "vs", inherit: true, rules: [],
  colors: {
    "editor.background": "#ffffff",
    "editorLineNumber.foreground": "#71809a",
    "editorLineNumber.activeForeground": "#52606d",
    "editor.lineHighlightBackground": "#f6f8fa",
    "editor.selectionBackground": "#dbeafe",
    "editorIndentGuide.background1": "#edf0f3",
    "editorCursor.foreground": "#2457d6",
  },
});
const keywords = "alignas alignof auto bool break case catch char class const constexpr continue default delete do double else enum explicit extern false float for friend if inline int long namespace new nullptr operator private protected public return short signed sizeof static struct switch template this throw true try typedef typename union unsigned using virtual void volatile while".split(" ");
monaco.languages.registerCompletionItemProvider("cpp", {
  provideCompletionItems(model, position) {
    const word = model.getWordUntilPosition(position);
    const range = {startLineNumber: position.lineNumber, endLineNumber: position.lineNumber, startColumn: word.startColumn, endColumn: word.endColumn};
    return {suggestions: [
      ...keywords.map(label => ({label, kind: monaco.languages.CompletionItemKind.Keyword, insertText: label, range})),
      ...[
        ["for", "for (int ${1:i} = 0; ${1:i} < ${2:n}; ++${1:i}) {\n    $0\n}"],
        ["if", "if (${1:condition}) {\n    $0\n}"],
        ["main", "int main() {\n    $0\n    return 0;\n}"],
      ].map(([label, insertText]) => ({label: `${label} …`, filterText: label, kind: monaco.languages.CompletionItemKind.Snippet, insertText, insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet, range})),
    ]};
  },
});

export function mount(textarea, {readOnly, fontSize = 14, onChange, onCursor}) {
  const focused = document.activeElement === textarea;
  const selection = [textarea.selectionStart, textarea.selectionEnd];
  const host = document.createElement("div");
  host.className = "code-editor";
  textarea.before(host);
  let editor;
  try {
    editor = monaco.editor.create(host, {
      value: textarea.value, language: "cpp", theme: "department", readOnly,
      ariaLabel: textarea.getAttribute("aria-label") || "Решение на C++17",
      fontFamily: "'Cascadia Code', Consolas, 'Liberation Mono', monospace", fontSize,
      lineHeight: 23, tabSize: 4, insertSpaces: true, detectIndentation: false,
      minimap: {enabled: false}, automaticLayout: true, scrollBeyondLastLine: false,
      padding: {top: 16, bottom: 16}, lineNumbersMinChars: 3,
      renderLineHighlight: "line", overviewRulerLanes: 0, hideCursorInOverviewRuler: true,
      wordBasedSuggestions: "currentDocument", suggest: {showWords: true},
      quickSuggestions: {other: true, comments: false, strings: false},
      fixedOverflowWidgets: true, stickyScroll: {enabled: false},
    });
  } catch (error) { host.remove(); throw error; }
  textarea.hidden = true;
  editor.onDidChangeModelContent(() => {
    textarea.value = editor.getValue();
    onChange();
  });
  editor.onDidChangeCursorPosition(event => onCursor(event.position));
  if (focused) {
    const model = editor.getModel();
    const start = model.getPositionAt(selection[0]), end = model.getPositionAt(selection[1]);
    editor.setSelection(new monaco.Selection(start.lineNumber, start.column, end.lineNumber, end.column));
    editor.focus();
  }
  return {
    setValue(value) { editor.setValue(value); },
    setReadOnly(value) { editor.updateOptions({readOnly: value}); },
    setFontSize(value) { editor.updateOptions({fontSize: value}); },
    layout() { editor.layout(); },
    focus() { editor.focus(); },
    reveal(line, column) {
      const model = editor.getModel();
      line = Math.max(1, Math.min(model.getLineCount(), line));
      column = Math.max(1, Math.min(model.getLineMaxColumn(line), column));
      editor.setPosition({lineNumber: line, column});
      editor.revealLineInCenter(line); editor.focus();
    },
  };
}

// The workbench starts first, so input and autosave also work during editor loading.
queueMicrotask(() => window.dispatchEvent(new Event("contest:editor-ready")));
