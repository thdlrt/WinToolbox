import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { native } from "../api";

export default function MemoryMarkdown({
  body,
  onError,
}: {
  body: string;
  onError: (error: unknown) => void;
}) {
  return (
    <article className="memory-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <a
              href={href}
              onClick={(event) => {
                event.preventDefault();
                if (href && /^https?:\/\//i.test(href))
                  void native.openExternal(href).catch(onError);
                else onError("请通过附件按钮打开本地文件。");
              }}
            >
              {children}
            </a>
          ),
          // Preview must not make third-party requests merely because a synced note embeds an image.
          img: ({ alt }) => (
            <span className="muted">图片：{alt || "请从附件打开"}</span>
          ),
        }}
      >
        {body || "暂无正文"}
      </ReactMarkdown>
    </article>
  );
}
