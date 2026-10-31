# Pacoragroma projects

Claude plugins and skills. Each folder is a self-contained project with its own README and license.

| Project | What it is |
|---|---|
| [day-trader](day-trader/) | 11 Claude skills that work like a small day-trading desk: they scan thousands of charts, track breaking news, time every entry and exit, and replay every outcome before you risk a dollar. US stocks, and crypto pairs such as BTC-USDT around the clock on Binance's public prices (or Coinbase's). Paper trading only. Free, MIT, no signup, no API key. |

## Install day-trader

In Claude Code, type these two commands yourself (Claude cannot run slash commands for you):

```text
/plugin marketplace add Pacoragroma/projects
/plugin install day-trader@pacoragroma-projects
```

Other ways to install are in [day-trader/README.md](day-trader/README.md#install): a manual copy
into `~/.claude/skills` (some folder names are generic, so it skips any that already exist), one zip
per skill for the Claude apps, and what actually happens when you paste the link and ask Claude to
install it. For the Claude apps and other sandboxes, also read
[Running in Claude's apps / sandboxes](day-trader/README.md#running-in-claudes-apps--sandboxes).
