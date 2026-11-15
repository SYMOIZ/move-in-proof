# Move-In Proof: demo video script (about 2 min 40 s)

The same lines appear as captions at the bottom of the screen during the automated run, so the
video explains itself even without a voice. Read them aloud if you want a voice-over.

| # | On screen | Say |
|---|---|---|
| 1 | Home page | "When a tenancy ends, the deposit argument is settled with phone photos, and nobody can prove when a photo was taken or whether it was edited. Move-In Proof fixes that." |
| 2 | Home page, three steps | "A tenant photographs the flat on move-in day, seals the photos, and the landlord co-signs. It runs entirely on serverless AWS: no servers, no pipeline." |
| 3 | Start a record | "I'm signed in with Amazon Cognito. I start a record for the flat." |
| 4 | Upload and label photos | "I add my move-in photos and label each one. They upload straight to Amazon S3." |
| 5 | Seal | "Now I seal the record. A Step Functions workflow fingerprints every photo with SHA-256 and locks it with S3 Object Lock. Nobody can overwrite or delete it, including me." |
| 6 | The seal card and stamp | "This is the record's fingerprint. If any photo, label or name changed, it would not match." |
| 7 | Landlord link | "The workflow is now waiting for the landlord, with nothing running and nothing billed. The landlord opens a link. No account needed." |
| 8 | Landlord agrees | "They look at the photos full size and agree or dispute, once. If they never answer, a timeout seals the record as tenant-only." |
| 9 | Co-signed | "Both sides are now on record." |
| 10 | Verify: match | "Months later, at move-out. I check a photo against the seal. It is fingerprinted in my browser and never uploaded. Match." |
| 11 | Verify: no match | "Here is the same photo with a single byte changed. No match." |
| 12 | Public link | "Anyone with the public link can verify, but only sees fingerprints, never the photos." |
| 13 | Closing | "API Gateway, Lambda, DynamoDB, S3 Object Lock, Step Functions and Cognito. One template, one command to deploy, and it costs nothing when idle." |

## Recording it

1. Sign in to the live app in the Chrome window that was opened for the demo.
2. Start a screen recording of that window. On Windows, Win+Alt+R starts and stops the built-in
   recorder; the file lands in Videos\Captures.
3. Say "go". The run takes about two and a half minutes and pauses on each caption.
4. Stop the recording, then upload it as a public or unlisted YouTube video and paste the link
   into the submission.

Optional extra shot for judges: the Step Functions execution graph in the AWS console, showing the
run parked on WaitForLandlord.
