---
slug: wordle-html
title: Build a Wordle clone
description: Wordle-style word-guessing game as one self-contained index.html page.
category: game-build
tags: [web, game, single-file, html]
suggested_adapter: miniloop
---

Build a playable Wordle clone as a single self-contained `index.html` file in the workspace root (all CSS and JavaScript inline — no external assets or CDN links).

Requirements:

- Pick a random 5-letter secret word from an embedded answer list of at least 200 common English words.
- The player gets 6 guesses. Each guess must be a valid 5-letter word from an embedded dictionary (which can be the answer list plus a modest set of additional valid guesses); reject non-words and wrong-length input with a clear visual shake/notice.
- After each guess, color every letter tile: green for correct letter in the correct position, yellow for the letter present elsewhere, gray for absent — with the correct duplicate-letter handling (a letter is only marked as many times as it appears in the answer).
- An on-screen keyboard that mirrors the tile colors (best-known state per key) plus physical keyboard input, Enter to submit, Backspace to delete.
- Win/lose end state revealing the answer, a restart button, and a guess-grid animation (tile flip or fade) when a guess is submitted.
- No build step, no dependencies: opening `index.html` in a browser must start the game.
