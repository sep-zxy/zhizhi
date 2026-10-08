import { useMemo } from 'react';
import hljs from 'highlight.js/lib/core';
import java from 'highlight.js/lib/languages/java';
import javascript from 'highlight.js/lib/languages/javascript';
import typescript from 'highlight.js/lib/languages/typescript';
import python from 'highlight.js/lib/languages/python';

hljs.registerLanguage('java', java);
hljs.registerLanguage('javascript', javascript);
hljs.registerLanguage('typescript', typescript);
hljs.registerLanguage('python', python);

export function highlightSource(text: string, path?: string | null) {
  const language = path?.endsWith(".java") ? "java" : path?.endsWith(".py") ? "python" : /\.tsx?$/.test(path ?? "") ? "typescript" : /\.[cm]?jsx?$/.test(path ?? "") ? "javascript" : undefined;
  return language ? hljs.highlight(text, { language }).value : hljs.highlightAuto(text).value;
}

export default function KnowledgeCode({ text }: { text: string }) {
  const highlighted = useMemo(() => hljs.highlightAuto(text).value, [text]);
  return <pre className="knowledge-numbered-code"><span className="knowledge-code-gutter" aria-hidden="true">{text.split('\n').map((_, index) => <span key={index}>{index + 1}</span>)}</span><code dangerouslySetInnerHTML={{ __html: highlighted }} /></pre>;
}
