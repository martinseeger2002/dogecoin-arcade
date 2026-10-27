# The LLM service: AI models and pictures on the exchange

Every arcade can sell, or give away, the AI models its operator runs on their own computer: language models (chat,
answers, code) and picture models. Players and apps on **every** arcade can use them, paying the seller directly in
the coins and tokens the exchange already trades. It all lives on the **Exchange → LLMs** tab, which is the first tab
of the Exchange.

## For players: using a model

### The model exchange

**Exchange → LLMs** is the model exchange: one card for every model an arcade shares, words (language models) and
pictures alike. Each card shows:

- the model's name, whether it makes **words** or **pictures**, and the seller (the operator's @name and their arcade),
- its **price** -- per token for words (**in** is what you send, **out** is what it writes), per credit for pictures,
- its **speed** (tokens a second, or seconds a picture), how much it was **used** in the last hour, and the longest reply
  or largest picture side it allows,
- how busy it is right now: **Idle**, **In use**, **Busy** (every slot taken) or **Full** (people waiting),
- **your credit** with that seller.

Tap **☆** to keep a model in **★ Favorites** -- the model exchange opens on your favorites when you have any. Choose
**All models**, **Words** or **Pictures**, search (the search always looks through every model), pick a coin, and
**Sort** by most used, cheapest, fastest, least busy or name.

**Tap a card to open that model's own page**: its card and prices, **Top up**, the chat (words) or **Make a picture**
(pictures), and API keys for it.

A model with no prices is **free for everyone**: sign in and use it, nothing to pay.

### Credit and top-ups

You pay a seller by **prepaid credit**:

1. Open the model and press **Top up**, pick a coin or token you hold, and enter an amount. The page says roughly how
   much it buys.
2. Your wallet shows the payment and its fee and asks before it signs, like any send.
3. The payment goes **straight to the seller's address**. Their arcade checks it on the chain, that it came from your
   key, and that nobody counted it before, and adds it to your balance there.

A coin payment counts as soon as the seller's node sees it -- usually within seconds of sending, no block needed.
An Omni token payment counts once the token index has read its block.
The seller's arcade keeps your balance, so top up small amounts with sellers you do not know yet.

You never choose a coin when you use a model: it takes whatever credit you hold with that seller. Your credit is shown
as **credits** for pictures (with the rate, for example 1 credit = 0.5 PEP) and as the tokens it still buys for words.

Each reply is charged after it is written, from the number of tokens the model server reports, and never takes your
balance below zero: a reply is cut short to what your credit still pays for.

### Chat

Open a words model: its page is a chat. The chat knows DogecoinArcade's own documentation: the seller's arcade adds
the parts of the docs that bear on your question, and those added words are **not charged** -- you pay only for your
own words and the reply. Under each reply you see the tokens in and out and what it cost. Untick **Arcade docs** to talk to
the model with nothing of the arcade's added. The conversation is kept in your browser, per model, until you press
**New chat**.

While a reply is being written, the chat follows it down only while you are at the bottom. Scroll up to read what it
wrote before and the page stays where you are; scroll back to the bottom and it follows again.

### Pictures

Arcades can also share a **picture model** (on ComfyUI). Pictures are priced in **credits**, by size: **1 credit for
every 250 pixels of the picture's longest side**, rounded up -- 250x250 is 1 credit, 251x251 is 2, 500x375 is 2,
1000x750 is 4. An edit of a picture you bring counts both ways: the credits of your picture going up plus those of the
new one coming down. Open the model, describe the picture, set any width and height (or pick a preset), optionally add
a picture of yours to change, and the button shows the exact credits before you press it. A picture that fails costs
nothing. Every picture you make is kept on your own arcade for **24 hours** -- even if you closed the page before it was
done -- under **Your pictures** on the model's page, to view, download or delete.
The same balance with a seller pays for words and pictures.

#### Inscribing a picture you made

Every picture under **Your pictures** can go on the chain from there:

- **Inscribe now** opens **Creator -> NFTs** with the picture already chosen and **This picture's details** filled in.
  Everything is optional:
  - a name;
  - a collection and its number, which name it "collection #number";
  - a description;
  - traits;
  - the model, kept as a trait (ticked by default);
  - the prompt (not ticked: it would be public on the chain for ever).

  The details go on the chain with the picture as its JSON, the same JSON every collection on the arcade uses, so its
  name and traits show in every wallet and on the Exchange. The page shows exactly what goes up.
- **As an ord** opens **Creator -> Ords** with the picture chosen, for Doginals, Pepinals, Bellinals and the other ord
  chains. An ord is the picture alone. To give ords a name and traits, inscribe them as a collection (below), even a
  collection of one.
- **A small collection.** Tick **For a collection** on each picture you want in it: a picked picture is kept for **a
  week** instead of a day (at most 40 at once), so a set can be made over a few days. Then press **Inscribe as NFTs**
  or **Inscribe as ords** on the bar above the pictures. The inscriber opens with every picked picture chosen, in the
  order you made them, numbered 1, 2, 3 ... Give the collection a name, a description and any traits every piece
  shares; it then goes on exactly as a folder of pictures would (see the [collections guide](collections.md)).

Inscribing costs the network's fees, and on a mainnet chain the operator's inscription fee too, if the arcade charges
one (Admin -> Trading -> Inscription fee). The wallet names both on their own lines before you sign.

### Using a model from your own programs (API keys)

A model's page has **Use it from your own apps**, which gives an **OpenAI-compatible API** on your own arcade:

- Base URL: `https://<your arcade>/v1`
- API key: press **Make a key** (name it, pick which seller and coin it spends). The key starts with `sk-arcade-` and
  is shown **once**; the curl example fills it in for you. Only a scrambled copy is kept; revoke a key at any time.

Any program that takes a base URL and an API key works: chat apps, coding assistants, scripts using the OpenAI
library. Endpoints: `/v1/models`, `/v1/chat/completions` (streaming or not, with tools), `/v1/images/generations` and
`/v1/images/edits`. A key spends one seller's credit in one coin.

### The arcade helper

The **robot** in the lower right corner of every page answers questions about the arcade, using the documentation and
knowing which page and tab you are on. It runs on a model shared with this arcade's operator, so you are not charged;
each person may ask 40 times an hour (a guest who is not signed in, 15). It shows only while such a model is online,
and appears within moments of one coming online. Its conversation is kept in your browser only.

On a computer you can drag the robot anywhere on the screen; the chat opens beside it and the spot is kept.
Double-click it to send it back to its corner. On a touch screen it stays in its corner.

It is not shown where a password or recovery words are typed, over a game in full screen, or over an app open on the
installed app's home screen. Turn it off with **Show the arcade helper** on **Your arcade**.

### Who you are to a seller, and what is safe

A seller knows you by your @name's address. When you use another arcade's model, your wallet signs, once a month, that
the seller may serve you through your own arcade. A top-up is proved by a signature from the key that paid. Your coins
never leave your wallet except as the top-ups you approve. The seller is trusted to count tokens honestly and to keep
your balance -- that is why top-ups go to the seller and the page suggests starting small.

## For operators: sharing your models

Everything is on **Admin → LLM**.

1. **Find models on this computer** looks at every model server on the machine -- vLLM, llama.cpp, Ollama, LM Studio,
   other OpenAI-compatible servers, and ComfyUI -- and lists their models. Tick the ones to share; the server address
   fills in by itself. A ComfyUI that starts on demand is offered with a **Wake it and look** button.
2. **Sell your model**: the server address (and an API key if the server needs one), the name buyers see, replies at
   once (others wait in line) and the longest reply.
3. **Your prices**: one row per coin or Omni token you accept, each with a price per input token and per output token.
   No prices means **free for everyone**; adding a price makes it paid, removing them all makes it free again.
4. **Share your image model**: ComfyUI's address, a **wake command** for a ComfyUI that sleeps until asked (it runs on
   your machine, so it is set at the node machine only), the largest side, quality steps, pictures at once, and a
   **price per credit**.
5. **Free for other operators**: name other operators (for example `@bigchiefenergy`) who use your models free --
   words and pictures, in chat, apps and API keys -- while everyone else pays your listed prices.
6. Tick **Tick to share on the exchange** and **Save changes**. Your offer reaches every linked arcade within seconds.

**Right now** shows how busy your model is, and **Usage analytics** opens a page with everything about its use:
totals, replies and tokens over time, earnings by coin, by way in (this arcade, API keys, other arcades), by model and
by buyer, and the latest replies. Top-ups arrive in your operator account's Wallet like any other payment, and your own
account always uses your models free.

The **Arcade helper** panel lists the models the robot may use for your players: your own, and any another operator
shares with you free. Tick each one it may use. Your own are ticked unless you untick them; a model shared by another
operator is used only once you tick it (and **Sign once to use it** with your wallet). It uses your own first, then the
least busy, and marks the one in use now. It never uses a paid model.
