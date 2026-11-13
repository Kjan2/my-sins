import renderMathInElement from 'katex/contrib/auto-render';
import 'katex/dist/katex.min.css';
for (const element of document.querySelectorAll('.problem-content')) {
  renderMathInElement(element, {
    delimiters:[{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false},{left:'\\[',right:'\\]',display:true},{left:'\\(',right:'\\)',display:false}],
    throwOnError:false, trust:false, strict:'warn', maxExpand:1000,
  });
}
