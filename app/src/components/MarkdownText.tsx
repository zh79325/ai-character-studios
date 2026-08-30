/**
 * Agent 回的 Markdown。
 *
 * 只挂 remark 插件、不开 rehype-raw：回答里夹的 HTML 一律当纯文本，模型吐个 `<script>` 也进不了
 * DOM。链接一律走新窗口，免得点一下把整个应用导航走。
 *
 * 排版规则写在 `index.css` 的 `.md` 那一组里——气泡空间小，块间距全压过一遍。
 */
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export default function MarkdownText({ text }: { text: string }) {
  return (
    <div className="md">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
        }}
      >
        {text}
      </Markdown>
    </div>
  )
}
