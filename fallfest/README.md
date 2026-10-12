# Braznarkel Ranch · Fall Fest 2026

Jackbox-style party games for the Braznarkel Ranch Fall Fest. One computer shows the game on a TV or projector. Everyone plays on their own phone by scanning a QR code. There are no apps to install, no accounts and no internet needed once the page loads, but all phones must be on the same Wi-Fi as the computer.

## The games

**Mix It Up** plays a random run of the games below back to back, with short rounds and one running leaderboard. Pick how many games (2–12) and press **Mix it up!** Between games the scoreboard shows for a few seconds, then the next game starts by itself. **Start next game** skips the wait.

| Game | How it plays | Players |
| --- | --- | --- |
| **Pumpkin Patch Pop Quiz** | Multiple-choice trivia across 10 categories: fall, geography, science, movies, music, sports, food, ranch life and history. Faster right answers score more. | 1+ |
| **Fact or Fiction** | Lightning round of true-or-false statements. 10 seconds each, with a bonus for streaks of right answers. | 1+ |
| **Fib-o-Lantern** | A strange true fact with a blank. Everyone writes a believable fake answer, then tries to find the truth. Points for the truth and for every friend you fool. | 2+ |
| **Guess the Gourd** | Every answer is a number. Closest guess wins, and an exact guess gets a bonus. | 1+ |
| **Line 'Em Up** | Tap four answers into the right order, like oldest to newest. Points for each one in the right spot. | 1+ |
| **Emoji Roundup** | Name the movie, song or thing from a few emoji. Type it in; small typos count. | 1+ |
| **Herd Mentality** | No right answers. Pick what you think most people will pick. Run with the herd to score. | 2+ |
| **Most Likely To** | "Who's most likely to get lost in the corn maze?" Vote for a player. Pick who the room picks to score. | 3+ |
| **Scarecrow Sketch** | One player draws a secret word on their phone while it shows live on the TV. Everyone else races to guess. | 2+ |
| **Hayride Head-to-Head** | Two players answer the same funny prompt and everyone else votes for the better one. | 3+ |

Scores carry across every game, so the lobby doubles as the fest-wide leaderboard.

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

## Branding

The ranch wordmark logo is `public/logo.svg` (a copy of `brand/braznarkel-ranch-logo.svg`). Replace that file to change the logo on every screen.

The event name and year come from the `brand` block at the top of `content.json`. They drive the badge, the big-screen title, the phone screens and the leaderboard. Change them for next year and they show up the next time the host goes back to the lobby:

```json
"brand": { "ranch": "Braznarkel Ranch", "event": "Fall Fest", "year": "2026" }
```

## Make your own questions

All questions live in `content.json`. Edit it with any text editor. Changes load the next time you start a game, with no restart needed.

```jsonc
{
  "trivia": [ { "cat": "Category", "q": "Question?", "a": "Right answer", "wrong": ["Wrong 1", "Wrong 2", "Wrong 3"] } ],
  "tf":     [ { "s": "A statement.", "t": true, "why": "Shown after the answer." } ],
  "order":  [ { "q": "Oldest to newest", "items": ["first", "second", "third", "fourth"] } ],
  "emoji":  [ { "e": "🦁👑", "a": "The Lion King", "cat": "Movie", "alts": ["Lion King"] } ],
  "herd":   [ { "q": "Best pie?", "options": ["Apple", "Pumpkin", "Pecan", "Cherry"] } ],
  "likely": [ "get lost in the corn maze" ],
  "draw":   [ { "w": "scarecrow", "alts": [] } ],
  "fib":    [ { "q": "A fact with a ____ in it.", "a": "the truth", "alts": ["other accepted spellings"], "decoys": ["fake used if few players", "another"] } ],
  "guess":  [ { "q": "How many…?", "a": 206, "unit": "bones" } ],   // leave unit "" for years
  "quip":   [ "A funny prompt for two players to answer" ]
}
```

## Ideas for more games

- **Costume Contest:** players upload a photo or describe a costume, and the crowd votes.
- **Haunted Hot Potato:** a bomb timer passes from phone to phone. Name something in a category to pass it on.
- **Harvest Bracket:** head-to-head votes on fall favorites (apple pie vs. pumpkin pie) until one wins.
- **Spooky Story Chain:** each player adds one line to a story, and the room votes on the best twist.
