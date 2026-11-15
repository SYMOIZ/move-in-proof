# AWS Builder Center: what to paste where

Everything below is ready to copy. The long write-up is in `Move-In-Proof-Submission.docx` (Word,
with all screenshots) and `../SUBMISSION.md` (the same text for GitHub).

## 1. Project title

```
Move-In Proof: move-in photos that neither tenant nor landlord can change later
```

## 2. Short description

```
Tenants seal move-in photos with Step Functions and S3 Object Lock, the landlord co-signs by link, and anyone can verify a photo later. 100% serverless.
```

## 3. Tags

```
serverless, aws-step-functions, aws-lambda, amazon-s3, amazon-dynamodb, amazon-cognito, amazon-api-gateway
```

Use the ones the site offers; the first five are the strongest match for this challenge.

## 4. The problem you picked and what your build does about it

```
When a tenancy ends, the security deposit is argued from phone photos. The tenant says the crack was there on day one; the landlord says the photo was taken last week. Neither file proves when it was made or whether it was edited.

Move-In Proof fixes the one missing piece: a copy of the move-in photos that neither side can alter, that both sides acknowledged at the start, and that anyone can check later.

A tenant signs in, uploads and labels photos, and presses Seal. A Step Functions workflow fingerprints every photo with SHA-256, locks that exact S3 object version with Object Lock, and writes a locked manifest. It then waits on a task token, with nothing running, for the landlord to co-sign through a link. No account is needed for the landlord. If they never answer, a timeout on the state seals the record as tenant-only. At move-out, anyone can pick a photo and the browser fingerprints it locally: the original gives Match, the same file with one byte changed gives No match. The file is never uploaded.
```

## 5. The serverless AWS services you used and how they connect

```
Amazon Cognito: registration, email confirmation, sign-in.
Amazon API Gateway (HTTP API): an open route plus a JWT-protected route (/api/my/*) in front of one Lambda.
AWS Lambda: one function serves the page and the JSON API; a second runs the workflow steps.
AWS Step Functions: seals the record. A Map state hashes and locks each photo, then a waitForTaskToken state waits for the landlord, with a timeout fallback.
Amazon DynamoDB (on-demand): record status; conditional writes stop double seals and double answers.
Amazon S3 with Versioning and Object Lock: photos, manifest.json and seal.json. Photos upload straight from the browser through presigned POSTs.

Flow: Browser -> API Gateway -> Lambda (api) -> DynamoDB / S3 / Step Functions. Step Functions -> Lambda (workflow) -> S3. When the landlord answers, the API calls SendTaskSuccess and the workflow resumes. Deployed with one SAM template and one command; no servers and no pipeline.
```

## 5b. Architecture diagram

Upload `architecture.png` from this folder.

## 6. Demo field (public video link)

```
https://www.youtube.com/watch?v=9Gith267CSc
```

## 7. Other links

```
Live app:  https://hpst16vp30.execute-api.us-east-1.amazonaws.com/
Code:      https://github.com/SYMOIZ/move-in-proof
```

## 8. Images to upload

All images are in `upload-images/`, numbered 01 to 17 in the order to upload them. Captions for each
are in `upload-images/INDEX.md`.

| Use | File |
|---|---|
| Cover or banner | `upload-images/01-banner.png` (or `17-banner-1200x630.png` for social previews) |
| Screenshots, in story order | `upload-images/02-home.jpg` through `upload-images/15-public-fingerprints.jpg` |
| Architecture diagram | `upload-images/16-architecture.png` |

## 9. Before you press Submit

- [ ] The YouTube video is **public or unlisted** and plays while you are signed out.
- [ ] The video is **under 3 minutes** (it is 1 min 41 s).
- [ ] The GitHub repo opens while you are signed out.
- [ ] The live app loads and "Create account" shows (judges need to register to try it).
- [ ] You are a Builder Center member aged 18 or over, and the project was not published before the challenge.
- [ ] Submit before **16 November 2026, 1:00 PM PT**.
