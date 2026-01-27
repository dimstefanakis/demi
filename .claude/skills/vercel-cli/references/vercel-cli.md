# Vercel CLI Overview

Vercel gives you multiple ways to interact with and configure your Vercel Projects. With the command-line interface (CLI) you can interact with the Vercel platform using a terminal, or through an automated system, enabling you to retrieve logs, manage certificates, replicate your deployment environment locally, manage DNS records, and more.

If you'd like to interface with the platform programmatically, check out the REST API documentation.

## Installing Vercel CLI

To download and install Vercel CLI, run one of the following commands:

```
pnpm i -g vercel
```

```
yarn global add vercel
```

```
bun add -g vercel
```

## Updating Vercel CLI

```
pnpm i -g vercel@latest
```

```
yarn global add vercel@latest
```

```
bun add -g vercel@latest
```

## Checking the version

```
vercel --version
```

## Using in a CI/CD environment

Vercel CLI requires you to log in and authenticate before accessing resources or performing administrative tasks. In a CI/CD environment, create a token and use the `--token` option.

## Available Commands

### alias
```
vercel alias set [deployment-url] [custom-domain]
vercel alias rm [custom-domain]
vercel alias ls
```

### bisect
```
vercel bisect
vercel bisect --good [deployment-url] --bad [deployment-url]
```

### blob
```
vercel blob list
vercel blob put [path-to-file]
vercel blob del [url-or-pathname]
vercel blob copy [from-url] [to-pathname]
```

### build
```
vercel build
vercel build --prod
```

### cache
```
vercel cache purge
vercel cache purge --type cdn
vercel cache purge --type data
vercel cache invalidate --tag foo
vercel cache dangerously-delete --tag foo
```

### certs
```
vercel certs ls
vercel certs issue [domain]
vercel certs rm [certificate-id]
```

### curl
```
vercel curl [path]
vercel curl /api/hello
vercel curl /api/data --deployment [deployment-url]
```

### deploy
```
vercel
vercel deploy
vercel deploy --prod
```

### dev
```
vercel dev
vercel dev --port 3000
```

### dns
```
vercel dns ls [domain]
vercel dns add [domain] [name] [type] [value]
vercel dns rm [record-id]
```

### domains
```
vercel domains ls
vercel domains add [domain] [project]
vercel domains rm [domain]
vercel domains buy [domain]
```

### env
```
vercel env ls
vercel env add [name] [environment]
vercel env update [name] [environment]
vercel env rm [name] [environment]
vercel env pull [file]
vercel env run -- <command>
```

### git
```
vercel git ls
vercel git connect
vercel git disconnect [provider]
```

### guidance
```
vercel guidance enable
vercel guidance disable
vercel guidance status
```

### help
```
vercel help
vercel help [command]
```

### httpstat
```
vercel httpstat [path]
vercel httpstat /api/hello
vercel httpstat /api/data --deployment [deployment-url]
```

### init
```
vercel init
vercel init [project-name]
```

### inspect
```
vercel inspect [deployment-id-or-url]
vercel inspect [deployment-id-or-url] --logs
vercel inspect [deployment-id-or-url] --wait
```

### install
```
vercel install [integration-name]
```

### integration
```
vercel integration add [integration-name]
vercel integration open [integration-name]
vercel integration list
vercel integration remove [integration-name]
```

### integration-resource
```
vercel integration-resource remove [resource-name]
vercel integration-resource disconnect [resource-name]
```

### link
```
vercel link
vercel link [path-to-directory]
```

### list
```
vercel list
vercel list [project-name]
```

### login
```
vercel login
vercel login [email]
vercel login --github
```

### logout
```
vercel logout
```

### logs
```
vercel logs [deployment-url]
vercel logs [deployment-url] --follow
```

### mcp
```
vercel mcp
vercel mcp --project
```

### microfrontends
```
vercel microfrontends pull
vercel microfrontends pull --dpl [deployment-id-or-url]
```

### open
```
vercel open
```

### project
```
vercel project ls
vercel project add
vercel project rm
vercel project inspect [project-name]
```

### promote
```
vercel promote [deployment-id-or-url]
vercel promote status [project]
```

### pull
```
vercel pull
vercel pull --environment=production
```

### redeploy
```
vercel redeploy [deployment-id-or-url]
```

### redirects
```
vercel redirects list
vercel redirects add /old /new --status 301
vercel redirects upload redirects.csv --overwrite
vercel redirects promote <version-id>
```

### remove
```
vercel remove [deployment-url]
vercel remove [project-name]
```

### rollback
```
vercel rollback
vercel rollback [deployment-id-or-url]
vercel rollback status [project]
```

### rolling-release
```
vercel rolling-release configure --cfg='[config]'
vercel rolling-release start --dpl=[deployment-id]
vercel rolling-release approve --dpl=[deployment-id]
vercel rolling-release complete --dpl=[deployment-id]
```

### switch
```
vercel switch
vercel switch [team-name]
```

### teams
```
vercel teams list
vercel teams add
vercel teams invite [email]
```

### target
```
vercel target list
vercel target ls
vercel deploy --target=staging
```

### telemetry
```
vercel telemetry status
vercel telemetry enable
vercel telemetry disable
```

### whoami
```
vercel whoami
```
