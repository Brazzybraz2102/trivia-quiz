# Braznarkel Trivia

A Jeopardy-style game for playing in person. It is a single web page with no install and no server.

## Run it

Open `jeopardy/index.html` in Chrome, Edge, Firefox or Safari. Put the window on a TV or projector (or cast the tab) and press **Fullscreen**.

## Set up a game

1. **Teams**: name your teams (up to 12). Teams 1–9 get a number key for buzzing in.
2. **Round One / Round Two**: type category names, clues and correct responses. Change the row values, add or remove rows and categories, add more rounds, and tick **Daily Double** on any clue.
3. **Final**: the category, clue and response for the final wager round.
4. **Save / Load**: everything saves automatically in your browser. Use **Copy game** to keep a board as text, and paste it back (or open a `.json` file) to load it.

The page starts with a sample game so you can try it right away.

## Hosting

| Action | How |
| --- | --- |
| Open a clue | Click a dollar tile |
| Buzz in | Players press their team number (1–9) |
| Start timer | `T` or the button |
| Reveal answer | `Space` |
| Score | ✓ adds the value, ✗ subtracts it (a correct mark ends the clue) |
| Back to board | `Esc` |
| Fix a score | Click the team's score and type the new number |
| Undo | **Undo score** reverts the last change |

Daily Doubles ask which team picked them and take a wager (up to the team's score, or the round's top value if that is higher). In the Final, teams with $0 or less sit out; the rest write wagers on paper, and the host enters them before marking right or wrong.
