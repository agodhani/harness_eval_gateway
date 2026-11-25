---
slug: snake-pygame
title: Build Snake in pygame
description: Classic Snake game implemented in Python with pygame.
category: game-build
tags: [python, game, pygame, desktop]
suggested_adapter: miniloop
suggested_rubric: code-default
---

Build the classic game Snake in Python using pygame.

Requirements:

- A `snake.py` (or small package) in the workspace root plus a `requirements.txt` pinning `pygame` (or `pygame-ce`) so it can be launched with `python snake.py` after installing requirements.
- Grid-based snake controlled with arrow keys and WASD: the snake moves at a fixed tick rate, grows by one segment when it eats food, and dies on wall or self collision.
- Food spawns on a random free cell — never on the snake.
- Score display (foods eaten), a game-over screen showing the final score, and restart on keypress (e.g. R or Enter) without restarting the process.
- Sensible defaults: ~600×600 window, ~20×20 cell grid, speed that makes the game playable.
- Clean structure: game loop, update, and render kept separate; no blocking `input()` calls or busy-wait sleeps outside the tick loop.
