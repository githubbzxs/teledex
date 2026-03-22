from __future__ import annotations

import unittest

from teledex.formatting import markdown_to_telegram_html, split_markdown_message


class FormattingTestCase(unittest.TestCase):
    def test_markdown_to_telegram_html_supports_common_blocks(self) -> None:
        markdown = (
            "# 标题\n\n"
            "- 列表项\n"
            "1. 序号项\n\n"
            "这是 **加粗**、*斜体*、`代码` 和 [链接](https://example.com)\n\n"
            "> 引用内容\n"
        )

        html = markdown_to_telegram_html(markdown)

        self.assertIn("<b>标题</b>", html)
        self.assertIn("• 列表项", html)
        self.assertIn("1. 序号项", html)
        self.assertIn("<b>加粗</b>", html)
        self.assertIn("<i>斜体</i>", html)
        self.assertIn("<code>代码</code>", html)
        self.assertIn('<a href="https://example.com">链接</a>', html)
        self.assertIn("&gt; 引用内容", html)

    def test_split_markdown_message_keeps_code_blocks_renderable(self) -> None:
        code_lines = "\n".join(f"print({index})" for index in range(40))
        markdown = f"前言\n\n```python\n{code_lines}\n```\n\n收尾"

        parts = split_markdown_message(markdown, 120)

        self.assertGreater(len(parts), 1)
        for part in parts:
            html = markdown_to_telegram_html(part)
            self.assertEqual(html.count("<pre><code>"), html.count("</code></pre>"))
            self.assertNotIn("```", html)


if __name__ == "__main__":
    unittest.main()
