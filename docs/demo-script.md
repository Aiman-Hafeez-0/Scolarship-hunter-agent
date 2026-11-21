# Demo script (2 minutes)

A screen recording that shows what Scholarship Hunter does and why you can trust it.

**Before you record**
- Log in to <https://scolarship-hunter-agent.vercel.app> first, so the video starts on Home.
- Open a recent digest email in another tab.
- Open the GitHub repository in a third tab.
- Hide anything personal (your email address, date of birth, CGPA). Do not open the Profile page.
- Browser width about 1280 px. Speak slowly. It is fine to pause the recording between scenes.

---

## Scene 1: The problem (0:00 - 0:15)

**Show:** the dashboard **Home** page. Do not click yet.

**Say:**
> "Scholarship deadlines are spread over dozens of university and government pages, and
> they change every year. I built Scholarship Hunter, an AI agent that checks these official
> pages for me and tells me what I can apply for, and when."

## Scene 2: Home (0:15 - 0:30)

**Show:** move the mouse over **Next deadlines**, then over **Last run**.

**Say:**
> "This is my dashboard. At the top are the opportunities I match, with the next deadlines.
> Here is the last run: it ran this morning by itself, on a schedule."

## Scene 3: The list (0:30 - 0:45)

**Click:** **Scholarships** in the top menu. Scroll down a little.

**Say:**
> "Here is everything the agent found. Each row shows whether I am eligible, maybe
> eligible or not, and how many days are left until the deadline."

## Scene 4: Detail with quotes (0:45 - 1:15)

**Click:** one scholarship that has a deadline. On its page:
1. Point at **Why this match**.
2. Under **Deadlines**, click **Show quote**.
3. Under **Requirements**, click **Show quote** on one more item.

**Say:**
> "This is the most important part. The AI reads the official page, but it is not allowed
> to make anything up. Every fact must come with the exact sentence from the page, and my
> code checks that this sentence really is on the page. If it is not, the fact is dropped.
> Here is the deadline, and here is the quote it came from.
> The matching itself is simple rules, not AI, so my personal details are never sent
> to an AI company. And here it explains why I match."

## Scene 5: The email (1:15 - 1:35)

**Click:** the email tab with the digest. Scroll slowly from **Action needed** to
**New matches**.

**Say:**
> "Every Monday I get this digest: what needs action soon, and what is new. I also get
> reminders 30, 14, 7 and 2 days before each deadline, so I never miss one."

## Scene 6: How it runs (1:35 - 1:55)

**Click:** the GitHub tab. Scroll to the **Architecture** diagram in the README.

**Say:**
> "It runs for free. GitHub Actions starts it every morning. It fetches pages politely:
> it follows robots.txt and waits five seconds between requests to the same site. Only pages
> that changed go to Gemini, with Groq as a backup. The results go into a Supabase database,
> and this dashboard on Vercel reads from it."

## Scene 7: Close (1:55 - 2:00)

**Show:** scroll to **Engineering decisions**, or go back to the dashboard Home.

**Say:**
> "The code is tested and open source. Thanks for watching!"

---

**Tips**
- Aim for 1:50 to 2:00. If you are too long, shorten scene 6.
- If a page loads slowly, pause the recording and continue when it is ready.
- Export at 1080p. Add the video link to the top of the main README when it is done.
