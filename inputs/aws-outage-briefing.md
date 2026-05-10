# AWS US-East-1 Outage — May 8, 2026

On May 8, 2026, Amazon Web Services experienced a major outage at its US-East-1
region (North Virginia data center campus), one of the largest cloud regions in
the world. CNBC reported that recovery would "take hours" and that consumer
trading platforms FanDuel and Coinbase were directly affected, with users unable
to place wagers or execute crypto trades during the disruption.

The incident began in the morning Eastern Time, with elevated error rates first
showing on EC2, Lambda, DynamoDB, and S3. The AWS Service Health Dashboard
acknowledged "increased latency and errors for multiple services in the
US-EAST-1 Region." Many downstream consumer apps — including streaming, fintech,
SaaS dashboards, and federal government portals that share US-East-1 — became
partially or fully unavailable.

Public reaction tracked four themes:

1. Concentration risk: critics argued that a single AWS region failing taking
   down banks, sportsbooks, exchanges and government simultaneously is a
   systemic-risk problem, not an AWS bug.
2. Multi-cloud vs. multi-region: engineers debated whether the cheaper fix is
   active multi-region within AWS, or true multi-cloud across AWS + GCP + Azure.
3. Coinbase and FanDuel pricing: prediction-market traders speculated about
   class-action lawsuits and whether earnings guidance for Q2 2026 would be cut.
4. AWS reputation: with another high-profile US-East-1 incident on the books,
   commenters asked whether enterprises would accelerate migration to GCP or
   Azure, or finally enforce multi-region by policy.

Coinbase did not immediately provide an outage compensation statement. FanDuel
acknowledged the issue and refunded some live bets that could not be settled.
Amazon’s stock (AMZN) traded down approximately 1.6% on the day before
recovering some of the loss into the close. Polymarket opened a market on
"Will AWS suffer another US-East-1 region-wide outage before December 31, 2026?"
at roughly 38% YES on the day of the incident.

Recovery completed late afternoon ET. AWS promised a postmortem within 14 days.
