# braznarkel.com

The homepage. One static page, hosted free on Netlify (project `braznarkel`,
https://app.netlify.com/projects/braznarkel), so it stays up whatever the PCs do.

Publish or update: zip `index.html` and `netlify.toml` (or just use the folder), open the project's
**Deploys** page and drag the zip into the drop box. `netlify.toml` tells Netlify to serve this folder as it
is (no build). The domain: **Domain management** in the project, and in Cloudflare's DNS an `A` record
`@ → 75.2.60.5` plus a `CNAME` record `www → braznarkel.netlify.app`, both "DNS only" (grey cloud).
The `nextbox` record stays: that's the Next Box tunnel.
