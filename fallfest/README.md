# Fall Fest Party Pack

Jackbox-style party games for a fall festival. One computer shows the game on a TV or projector. Everyone plays on their own phone by scanning a QR code. There are no apps to install, no accounts and no internet needed once the page loads, but all phones must be on the same Wi-Fi as the computer.

## The games

| Game | How it plays | Players |
| --- | --- | --- |
| **Pumpkin Patch Pop Quiz** | Multiple-choice trivia. Faster right answers score more (500–1,000). | 1+ |
| **Fib-o-Lantern** | A real but strange fact with a blank. Everyone writes a believable fake answer, then tries to find the truth among the fakes. +1,000 for finding the truth, +500 for each person your fib fools. | 2+ (best with 4+) |
| **Hayride Head-to-Head** | Each player gets two funny prompts, and each prompt goes to two players. Everyone else votes for the better answer. Up to 1,000 per matchup, +250 for a clean sweep. | 3+ |
| **Guess the Gourd** | Every answer is a number. Closest guess gets 1,000, then 600 and 300. An exact guess gets 1,500. | 1+ |

Scores carry across games, so the lobby doubles as the fest-wide leaderboard.

## Run it

1. Install **Node.js** (the LTS version) from <https://nodejs.org>. Nothing else is needed.
2. Start the game:
   - **Mac:** double-click `start-mac.command`. If macOS blocks it, right-click it, choose **Open**, then **Open** again.
   - **Windows:** double-click `start-windows.bat`.
   - **Any computer:** open a terminal in this folder and run `node server.js`.
3. The big screen opens at `http://localhost:3000/host`. Put that window on the TV and press **Fullscreen**.
4. Players scan the QR code on the screen, or type the address shown under it, then pick a name and icon.

To run the host screen from a different device (a tablet, say), use the `/host?key=…` link the terminal prints when it starts.

### Wi-Fi tips for the event

- Phones must be on the **same network** as the computer. Phones on cellular data can't connect.
- Some venue and guest Wi-Fi networks block devices from reaching each other. If phones can't open the address, turn on your phone's **personal hotspot**, connect the laptop to it, and have players join that hotspot. A travel router works too, and handles crowds better.
- If the computer's firewall asks whether to allow Node.js on the network, choose **Allow**.
- To use a different port: `PORT=8080 node server.js`.

## Hosting

- Pick a game in the lobby, use − / + to set the length, and press **Play**.
- Each phase has a timer and moves on by itself as soon as everyone has answered. **Skip ahead** moves on right away.
- **End game** goes back to the lobby at any time. **Back to lobby** appears after the final scoreboard.
- Click the ✕ next to a player in the lobby to remove them. **Reset scores** zeroes the leaderboard, and **Clear players** empties the lobby before a new group.
- Sounds start after your first click on the host screen. **Sound on/off** toggles them.
- Typed answers pass through a basic family-friendly word filter before they reach the big screen. Keep an eye out anyway.
- Players who refresh or lock their phone rejoin automatically as the same person.

## Make your own questions

All questions live in `content.json`. Edit it with any text editor. Changes load the next time you start a game, with no restart needed.

```jsonc
{
  "trivia": [ { "q": "Question?", "a": "Right answer", "wrong": ["Wrong 1", "Wrong 2", "Wrong 3"] } ],
  "fib":    [ { "q": "A fact with a ____ in it.", "a": "the truth", "alts": ["other accepted spellings"], "decoys": ["fake used if few players", "another"] } ],
  "guess":  [ { "q": "How many…?", "a": 206, "unit": "bones" } ],   // leave unit "" for years
  "quip":   [ "A funny prompt for two players to answer" ]
}
```

## Ideas for more games

These came out of the brainstorm and would fit the same setup:

- **Scarecrow Sketch:** one player draws a secret fall word on their phone, and everyone else guesses (Drawful style).
- **Most Likely To…:** "Who's most likely to get lost in the corn maze?" Everyone votes for another player.
- **Costume Contest:** players upload a photo or describe a costume, and the crowd votes.
- **Haunted Hot Potato:** a bomb timer passes from phone to phone. Answer a category to pass it on.
- **Harvest Bracket:** head-to-head votes on fall favorites (apple pie vs. pumpkin pie) until one wins.
- **Spooky Story Chain:** each player adds one line to a story, and the room votes on the best twist.
