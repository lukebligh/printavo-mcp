# Clint's Claude Setup — Voice, 5 Skills, 5 Weekday Scheduled Tasks
Est Merch · Oct 20, 2026

Everything here runs from **Clint's** Claude account with his own connectors (Outlook as clint@, Pipedrive, Slack, the Printavo connector). Drafts land in **clint@ Drafts**. Nothing sends on its own.

| Piece | What it is | How Clint uses it |
|---|---|---|
| `clints-voice` | His email voice, learned from his own sent mail | Built once with the prompt in Step 1. Every draft uses it. |
| `clint-start-my-day` | Morning run: at-risk jobs for the 8:00 meeting, overnight email, website leads logged by 9 | "start my day" |
| `clint-sales-sweep` | Quotes not out, quotes not approved, leads, Lead Drop, fall list, Pipedrive hygiene | "sales sweep" / "who should I call" |
| `clint-cx-sweep` | Dates at risk, stuck jobs, proofs waiting, customers due an update, promo with Mechelle | "CX sweep" / "check my jobs" |
| `clint-shop-pulse` | Daily and weekly report to Luke: what needs him, sales, production, customers, Rocks, margin watch | "shop pulse" / "L10 prep" |
| `clint-sundown` | Sundown Rule: a reply drafted for every unanswered email + end-of-day scorecard | "sundown" |

**The work runs on a schedule. The skills are for on-demand use in between.** Every email-producing job is a weekday scheduled task that drafts into clint@ Drafts (and a Slack draft for the shop pulse). Clint reviews and sends; nothing sends on its own.

| Scheduled task (weekdays, Central) | What lands | Same logic as skill |
|---|---|---|
| A. Morning Inbox, 7:20 AM | Reply drafts for overnight email + Slack DM: leads to call, at-risk jobs for 8:00 | start-my-day |
| C. Sales Follow-Ups, 10:15 AM | Follow-up drafts on quotes not approved, stale deals and Lead Drop leads + margin flags | sales-sweep |
| D. Customer Updates, 12:45 PM | Job-update, at-risk heads-up, proof-nudge and ready-for-pickup drafts | cx-sweep |
| B. Sundown Drafts, 3:45 PM | A reply drafted for every unanswered email + scorecard DM | sundown |
| E. Shop Pulse, 4:05 PM (Friday = weekly) | Draft Slack message to Luke in Clint's Slack, ready to send | shop-pulse |

**No overlap with Luke:** Luke's tasks draft only on mail addressed to luke@. Clint's draft on mail to clint@ and on unanswered sales@ items. Luke's owner emails to website leads stay Luke's.

---

## Step 1 — Build his voice skill (one chat, ~10 min)

Paste this into a new chat in Clint's Claude:

```
Build me an email voice skill called "clints-voice" from my real sent mail, so every draft you write for me sounds like I typed it.

1. Read at least 80 of my sent emails in Outlook (clint@estmerch.com, Sent Items, last 6 months). Get a spread: customers (quotes, order updates, problems), prospects and cold follow-ups, vendors, and internal mail to Luke, Mechelle and the crew. Skip anything that is a template, an auto-reply, or text I quoted from someone else.
2. Also read my last 2 months of Slack messages if Slack is connected.
3. Write a SKILL.md with these sections, using direct quotes from my real emails as examples (strip customer phone numbers and prices):
   - How to draft (step by step)
   - Core voice (how I come across, 5–7 points)
   - Mechanics (punctuation, sentence length, capitalization, abbreviations, emoji, how I format numbers and dates)
   - Openers I actually use, closers I actually use, and my sign-off/signature exactly as it appears
   - By audience: customers, prospects, vendors/contract partners, team. 2–3 real examples each
   - "When something went wrong" (how I apologize and fix)
   - Banned: phrases I never use, plus "hope this finds you well", "circling back", "touching base", "don't hesitate to reach out", "I'd be happy to assist", em dashes
   - A final checklist before handing over a draft
   - A note that Microsoft 365 drafts don't get my automatic Outlook signature, so the signature has to be added as plain HTML (tags div, p, br, b, a only, no style attributes)
4. Show me the draft skill, then save it as a skill on my account named clints-voice. Name and description: "clints-voice: Write emails and messages as Clint at Est Merch in his own voice. Use whenever drafting anything Clint will send."
```

When Clint edits a draft, he tells his Claude what he changed and to add it to clints-voice. It gets better every week.

## Step 2 — Add the 5 skills

Upload each zip in Clint's Claude (Settings → Capabilities → Skills): `clint-start-my-day.zip`, `clint-sales-sweep.zip`, `clint-cx-sweep.zip`, `clint-sundown.zip`, `clint-shop-pulse.zip`.

## Step 2b — Pricing access
Clint needs the cost, margin and contract pricing documents. Add him to the restricted pricing project (or put the contract price lists and cost-per-imprint numbers in a project he's in). The Playbook instructions v2 now allow costs and margins for Luke and Clint only.

## Step 3 — Create the 5 scheduled tasks

In Clint's Claude, paste each into a chat with: *"Create a scheduled task named [name] that runs [schedule] Central time with this exact prompt:"*. Set it to approve automatically if offered (it runs with no one watching). Create all five; Clint shouldn't have to remember to run anything that produces an email.

### Task A — Clint Morning Inbox (weekdays 7:20 AM CT)

```
Run Clint's morning inbox pass for Est Merch (Established Merch, St. Louis print shop). You are unattended: do not ask questions. NEVER send an email. Create Outlook DRAFTS only, in clint@estmerch.com. Never change anything in Pipedrive or Printavo. The only message you send is one Slack DM to Clint (user ID U07VDHDSMUJ).

# Step 0: Load Printavo
Printavo is a custom connector that is often slow to load when a scheduled run starts. Run ToolSearch "select:mcp__Printavo__get_production_schedule,mcp__Printavo__search_orders,mcp__Printavo__get_order_details". If nothing loads, `sleep 60` in Bash and retry, up to 5 tries. If it never loads, skip the at-risk section and say so in one line. Never claim the connector is unsupported.

# Step 1: Voice
Load the clints-voice skill and follow it for every draft. If it isn't available: read Clint's 10 most recent sent customer emails and match his tone and length; copy his signature exactly from them. Never use "hope this finds you well", "circling back", "touching base", "don't hesitate to reach out", em dashes, or bullet lists.
Drafts made through Microsoft 365 do NOT get the automatic Outlook signature: add it yourself. bodyType "html", allowed tags only div, p, br, b, a, NO style/class/id attributes (otherwise the draft is rejected).

# Step 2: clint@ (DRAFT)
Window: mail since 2:00 PM Central the previous weekday (Monday: since Friday 2:00 PM), plus any thread from the last 3 days where the newest message is from outside estmerch.com and nobody at estmerch.com has replied.
Draft only where clint@estmerch.com is on the To line (Luke's own task handles mail to luke@). Keep customers, prospects, vendors and contract partners, and Printavo messages where a customer declined art or asked a question. Skip newsletters, pitches, receipts, notifications, internal estmerch.com mail, threads already answered, and threads that already have a draft in clint@ Drafts.
Read the full thread. Use only facts in it, plus Printavo for status, due date, tracking or invoice link (search_orders returns OLDEST FIRST and caps results: use limit 50+ and read the end). Never invent prices, dates, or promises; use a bracketed placeholder like [price?] and flag it. Reply (or reply-all when others at the customer are on it). One draft per thread.

# Step 3: sales@ (flag; draft from clint@ only for customers waiting)
Read sales@ with mailboxOwnerEmail = sales@estmerch.com.
- New website leads ("New Inquiry: Get a Quote"): flag name, company, what they want, submitted time, phone. Skip anything marked TEST. Do not draft: these get a call.
- Customer emails waiting on us (newest from outside, no estmerch.com reply, including luke@ Sent): draft a reply from clint@ to the sender, subject "RE: [their subject]", same rules as Step 2.
- Urgent: upset customer, declined proof, in-hands date at risk: flag.
Ignore pitches, vendor promos, and Printavo status/payment notifications.

# Step 4: Website leads with no logged contact
Website leads from the last 2 business days: check Pipedrive (searchPersons / searchLeads / searchDeals, getActivities, getNotes) for any call, email, note or activity logged after submission. Luke's automatic owner email ("Your ... quote" from luke@ minutes after the form) does NOT count; it told them Clint would call. List every lead with nothing logged.

# Step 5: At-risk jobs for the 8:00 production meeting
Printavo get_production_schedule for today through the next 5 business days, plus get_order_details: jobs where the scheduled date is after the customer due date, or due within 3 business days with goods, art or proof approval not done. Direct customers first. Max 8.

# Step 6: Slack DM to Clint (U07VDHDSMUJ)
*Morning: N drafts ready in clint@ Drafts*
• *Name / Company*: what they need, one line. [NEEDS YOU: x] if there's a placeholder
*Call first: website leads with no contact logged* (only if any)
• *Name / Company*: submitted [day time], phone, what they want
*For the 8:00 meeting: at-risk jobs* (only if any)
• #[order] *Customer*: due [M/D], [what's missing or late]
*Heads up* (only if any): urgent items, one line each
End with: "Skim, send, and log every call in Pipedrive before 9."
If nothing needs him: "Morning: inbox clear, no unlogged leads, nothing at risk."
Only report facts you verified with a tool in this run. If a tool fails, still send what you can and say what failed in one line.
```

### Task B — Clint Sundown Drafts (weekdays 3:45 PM CT)

```
Run the Sundown Rule for Clint at Est Merch. The rule: every email that needs a response gets one before Clint leaves today, even if it's just "got it, I'll get you an answer tomorrow." You are unattended: do not ask questions. NEVER send an email. Create Outlook DRAFTS only, in clint@estmerch.com. Never change anything in Pipedrive or Printavo. The only message you send is one Slack DM to Clint (user ID U07VDHDSMUJ).

# Step 0: Load Printavo
ToolSearch "select:mcp__Printavo__get_production_schedule,mcp__Printavo__search_orders,mcp__Printavo__get_order_details". If nothing loads, `sleep 60` and retry up to 5 times. If it never loads, still draft holding replies (no real answers that need Printavo) and say so in one line.

# Step 1: Voice
Load the clints-voice skill and follow it. If it isn't available: read Clint's 10 most recent sent customer emails, match his tone, copy his signature exactly. bodyType "html", tags div, p, br, b, a only, NO style/class/id attributes. No em dashes, no bullet lists, no "hope this finds you well" or "circling back".

# Step 2: Find everything still waiting
clint@ inbox and sales@ (mailboxOwnerEmail = sales@estmerch.com), last 3 business days: threads where the newest message is from outside estmerch.com (customer, prospect, vendor, contract partner) and nobody at estmerch.com has replied since. Check luke@ Sent if you can see it so nothing gets double-answered.
Skip: newsletters, pitches, receipts, automated notifications, Printavo status emails, website-lead form emails (those get a call), anything marked TEST, and threads that already have a reply draft in clint@ Drafts.
Never contact End of The Line Cafe or Auto Art.

# Step 3: Draft a reply for every one (no cap)
On the thread from clint@ (for sales@ items: a reply from clint@ to the sender, subject "RE: [their subject]").
- If the answer is in Printavo or the thread (status, due date, tracking, invoice link, a yes/no that's already decided): write the real answer. search_orders returns OLDEST FIRST and caps: use limit 50+ and read the end.
- If it needs a price, a decision, or someone else: write a short holding reply that says what happens next and when, e.g.
  "Got it… I'll get you an answer tomorrow."
  "Got your art, I'll have a proof back to you tomorrow."
  "Checking with the crew on that date, I'll let you know first thing tomorrow."
  On Friday say "Monday" instead of "tomorrow".
- Never promise a price, date or capability that isn't in the data. Use [price?] / [date?] placeholders and flag them.

# Step 4: Tomorrow's at-risk jobs
Printavo get_production_schedule for the next business day and the 3 after, plus get_order_details: scheduled date after the customer due date, or due within 3 business days with goods, art or proof not done. Max 8.

# Step 5: Pipedrive quick count
Clint = owner_id 26695756. Count his open leads (Leads inbox) and open deals (pipeline 2) with NO next activity. Name the top 3 by value.

# Step 6: Slack DM to Clint (U07VDHDSMUJ)
*Sundown: N replies drafted in clint@ Drafts*
• *Name / Company*: what they asked, [answer / holding: "answer tomorrow"]
*Tomorrow at 8: at-risk jobs* (only if any)
• #[order] *Customer*: due [M/D], [issue]
*No next action*: N leads, N deals. Top 3: [names, $]
End with: "Send the drafts before you go. Anything answered 'tomorrow' goes on tomorrow's list."
If nothing is waiting: "Sundown: everyone's been answered."
Only report facts you verified with a tool. If a tool fails, say what failed in one line.
```

### Task C — Clint Sales Follow-Ups (weekdays 10:15 AM CT)

```
Run Clint's sales follow-up drafts for Est Merch. You are unattended: do not ask questions. NEVER send an email. Create Outlook DRAFTS only, in clint@estmerch.com. Never change anything in Pipedrive or Printavo. The only message you send is one Slack DM to Clint (user ID U07VDHDSMUJ).

# Step 0: Load Printavo
ToolSearch "select:mcp__Printavo__search_orders,mcp__Printavo__get_order_details,mcp__Printavo__get_outstanding_balances". If nothing loads, `sleep 60` and retry up to 5 times. If it never loads, draft nothing and DM Clint one line saying so (without Printavo the recent-order check can't run).

# Step 1: Voice
Load the clints-voice skill. Fallback: read Clint's 10 most recent sent customer emails, match his tone, copy his signature exactly. bodyType "html", tags div, p, br, b, a only, NO style/class/id. No em dashes, no bullet lists, no "circling back / touching base / hope this finds you well".

# Step 2: Margin check (flag only)
Printavo quotes created since yesterday 10 AM: check each against the margin floor and price rules (Nov 1, 2026 increase +7% direct, +8–10% contract; DTF and InkSoft at 55% gross margin; contract jobs on the contract price list), using the documented Est Merch costs in the Playbook / pricing project. Never use an industry default. List any below floor with the number and what would fix it.

# Step 3: Find who needs a follow-up (Clint = Pipedrive owner 26695756)
a. Quotes sent 2+ business days ago, not approved (Printavo). 
b. Open deals in pipeline 2, stages 6 Qualified and 7 Building Quote, with no activity, note, email or stage change in 3+ business days.
c. Lead Drop leads (Pipedrive Leads inbox) added 2+ business days ago that have a logged first touch but no reply and no activity since.
For every candidate, skip if ANY is true:
- Printavo search_orders (company AND contact name, limit 50+, results are OLDEST FIRST so read the END) shows an order or quote created in the last 14 days after the deal/quote in question, or any order in the last 90 days for a deal: list it under "Update in Pipedrive" instead.
- Anyone at estmerch.com emailed them in the last 3 business days (search clint@, luke@ if visible, and sales@ with mailboxOwnerEmail = sales@estmerch.com, by their email).
- A draft to them already exists in clint@ Drafts.
- Contract/trade account getting anything but a quote follow-up; End of The Line Cafe; Auto Art; an unpaid balance in Printavo get_outstanding_balances (list it instead).
- No email address: list under "Call, no email" with phone.
Rank by dollar value. Max 6 drafts.

# Step 4: Draft (from clint@ to the contact; reply on the existing thread when one exists)
Use only facts from Printavo, Pipedrive and the thread. No prices, no discounts. 2–4 short lines:
- Quote out: "[Name], wanted to make sure the quote for the [job] came through. [If an in-hands date is known: To have them by [date] we'd want approval by [date minus 14 days].] Any questions, just reply here."
- Stale deal: "[Name], following up on the [what the deal is for]. Still on for [timing, if known]? I can get a quote back to you today."
- Lead Drop: one line tying back to their trigger (from the Pipedrive lead note) + one easy question.

# Step 5: Slack DM to Clint
*Sales follow-ups: N drafts in clint@ Drafts*
• *Company* · $value · [quote out N days / no touch since M/D / lead drop]
*Below margin floor* (only if any): quote #, customer, margin, fix
*Update in Pipedrive* (only if any): looks won/active: #order
*Call, no email* (only if any)
End with: "Skim, send, then call the top 3."
Only report facts you verified with a tool. If a tool fails, say what failed in one line.
```

### Task D — Clint Customer Updates (weekdays 12:45 PM CT)

```
Draft Clint's customer job updates for Est Merch. You are unattended: do not ask questions. NEVER send an email. Create Outlook DRAFTS only, in clint@estmerch.com. Never change anything in Printavo or Pipedrive. The only message you send is one Slack DM to Clint (user ID U07VDHDSMUJ).

Why: once a job is in a production lane the crew moves the statuses and Printavo's automations email the customer. Clint adds the human touch, and if a date is at risk the customer hears it from us first.

# Step 0: Load Printavo
ToolSearch "select:mcp__Printavo__get_production_schedule,mcp__Printavo__search_orders,mcp__Printavo__get_order_details,mcp__Printavo__get_recent_orders". If nothing loads, `sleep 60` and retry up to 5 times. If it never loads, draft nothing and DM Clint one line saying so.

# Step 1: Voice
Load the clints-voice skill. Fallback: read Clint's 10 most recent sent customer emails, match his tone, copy his signature exactly. bodyType "html", tags div, p, br, b, a only, NO style/class/id. No em dashes, no bullet lists.

# Step 2: Pick the jobs (max 8)
From get_production_schedule (today through the next 7 business days), get_recent_orders and get_order_details:
1. AT RISK: scheduled date after the customer due date, or due within 3 business days with goods, art or proof approval not done.
2. PROOF WAITING: proof sent 2+ business days ago, not approved.
3. DUE THIS WEEK, in progress, no email from anyone at estmerch.com in the last 2 business days (search clint@, luke@ if visible, and sales@ with mailboxOwnerEmail = sales@estmerch.com, by the customer's email).
4. READY: completed 2+ business days ago, not picked up or shipped.
Skip: End of The Line Cafe, Auto Art, jobs that already have a draft in clint@ Drafts, and jobs with an open problem (misprint, short-ship, complaint): list those for Clint instead. Contract/trade accounts get only AT RISK heads-ups (to the contract customer), nothing else. Order of priority: at risk, proof waiting, due soonest, biggest total.

# Step 3: Draft (from clint@ to the order's contact; reply on the latest thread about the order when one exists)
Only facts from Printavo. Never invent a date or a reason; use [new date?] and flag it. 2–3 short lines:
- At risk: "[Name], heads up on your [job]… [real reason in plain words]. [Fix and new date only if known.] Wanted you to hear it from us first."
- Proof waiting: "[Name], your proof for the [job] is waiting on your OK. Approve by [day] and we stay on track for [date]."
- On track: "[Name], quick update on your [job]… [where it is], still on track for [date]. I'll let you know the minute it's ready."
- Ready: "[Name], your [job] is ready. [Pickup or shipping, from the order]."

# Step 4: Slack DM to Clint
*Customer updates: N drafts in clint@ Drafts*
• #[order] *Customer* · [AT RISK / proof / on track / ready] · due M/D
*Hold off* (only if any): open problems, one line each
*Ask Mechelle* (only if any): promo orders waiting on sourcing or tracking
End with: "Skim, fill any [brackets], send."
If nothing qualifies: "Customer updates: nothing needs a note today."
If a tool fails, say what failed in one line.
```

### Task E — Clint Shop Pulse (weekdays 4:05 PM CT; Fridays = weekly)

```
Build the Est Merch shop pulse that Clint sends Luke. You are unattended: do not ask questions. READ ONLY in Printavo, Pipedrive and Outlook: never change anything, never send email. Do NOT send the pulse to Luke yourself: create it as a Slack DRAFT in Luke's DM (Luke = U0504FSDYB0) using the Slack draft tool, so Clint reviews and sends it. Then send Clint (U07VDHDSMUJ) one line: "Shop pulse draft is in your Slack DM with Luke. Review and send." If the Slack draft tool isn't available, DM the full pulse text to Clint instead, starting "Draft for Luke:".

# Step 0: Load Printavo
ToolSearch "select:mcp__Printavo__get_production_schedule,mcp__Printavo__get_recent_orders,mcp__Printavo__get_order_details,mcp__Printavo__search_orders". If nothing loads, `sleep 60` and retry up to 5 times. If it never loads, build the pulse without Production and say so.

# Daily pulse (Mon–Thu)
*Shop pulse [Day M/D]*
*Needs you* (only if any, first): decisions only Luke can make: a quote below the margin floor, a make-it-right beyond a free reprint, a big customer problem, a staffing issue. If none: "Nothing needs you today."
*Sales*: quotes sent today (N, $) · approved today (N, $) · website leads today (N; all contacted? avg minutes to first logged contact) · Lead Drop N/5 touched · fall list: $ won toward $105,437 (Clint's open deals won since Oct 1).
*Production*: jobs shipped/picked up today (N) · at risk this week (#order, customer, why) · misprints or reprints today (N, cause).
*Customers*: anyone upset or still waiting, and what's being done.
*Tomorrow*: the 1–3 things that matter most.

# Weekly pulse (Friday): the same for the whole week, plus
*Rocks*: one word each with the number behind it (Rock 2 is Clint's: $ won, deals open, deals touched this week).
*Scorecard*: website leads and speed to first contact · quotes sent vs approved · Lead Drop touched · open leads/deals with no next action (target 0) · on-time jobs · misprints.
*Issues for Monday's L10*: top 3, one line each, with an owner.
*Margin watch*: quotes sent below floor this week (target 0), jobs where cost ran over.

# Rules
Sources: Printavo (search_orders is OLDEST FIRST and capped: limit 50+, read the end), Pipedrive (Clint = owner 26695756, pipeline 2), Outlook (clint@ and sales@ via mailboxOwnerEmail), Slack #cx-daily (C0BJ5N0M2TV).
Only numbers a tool showed you; leave out anything you couldn't pull and say so. A stale record isn't proof something didn't happen: write "the record shows X". Costs and margins are fine (Luke and Clint only). Never include employee pay or HR details. Leave out cash and bank balances.
```
