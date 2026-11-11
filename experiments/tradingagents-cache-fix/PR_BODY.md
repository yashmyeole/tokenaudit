Refs #750.

The bull/bear researchers and the three risk debaters all send the same reports (and in the risk debate, the same trader plan and history), but each prompt opens with its own role paragraph. Provider prompt caches match from the first byte, so none of these calls can reuse the previous turn's prefix and every one bills at the full input rate.

This moves the shared part to the front and the role text to the end. The wording is unchanged except "the sources above" where it used to say "the following sources".

To check it, I ran the five nodes against a stub LLM at the default config (one debate round, one risk round) and compared each prompt with the earlier ones. Every turn after the first now starts with the previous turn's full shared block (reports, trader plan, debate history). Before, turns from different roles shared at most 11 leading characters.

On live runs in August (gpt-5.4-mini, three matched pairs, an earlier version of this change on a33fd4c), the run's cache hit rate went from 7.5% to 32.4%. Setup and raw numbers: https://github.com/yashmyeole/tokenaudit/blob/main/docs/case-studies/tradingagents.md. I'll add a fresh pair on current main as a comment.

I left the analyst prompts alone. v0.7.1 leads them with the date on purpose so tool calls anchor to it, and the earlier version of this PR moved it.

The test suite passes locally (1412 passed).
