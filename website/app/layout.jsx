import { Footer, Layout, Navbar } from 'nextra-theme-docs'
import { Head } from 'nextra/components'
import { getPageMap } from 'nextra/page-map'
import 'nextra-theme-docs/style.css'
import './globals.css'

export const metadata = {
  metadataBase: new URL('https://diffcontext.dev'),
  title: {
    template: '%s — DiffContext',
  },
  description:
    "Find the code that matters for a change, and fit it into an LLM's context window — automatically.",
  applicationName: 'DiffContext',
  openGraph: {
    url: 'https://diffcontext.dev',
    siteName: 'DiffContext',
    locale: 'en_US',
    type: 'website',
  },
  twitter: {
    card: 'summary_large_image',
    site: 'https://diffcontext.dev',
  },
}

export default async function RootLayout({ children }) {
  const pageMap = await getPageMap()

  return (
    <html lang="en" dir="ltr" suppressHydrationWarning>
      <Head faviconGlyph="⚡" />
      <body>
        <Layout
          navbar={
            <Navbar
              logo={
                <span style={{ fontWeight: 800, fontSize: '1.1rem', letterSpacing: '-0.02em' }}>
                  ⚡ DiffContext
                </span>
              }
              projectLink="https://github.com/trakshan-mishra/Diffcontext"
            />
          }
          footer={
            <Footer>
              <span>
                MIT {new Date().getFullYear()} ©{' '}
                <a
                  href="https://github.com/trakshan-mishra/Diffcontext"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  DiffContext
                </a>
              </span>
            </Footer>
          }
          pageMap={pageMap}
          docsRepositoryBase="https://github.com/trakshan-mishra/Diffcontext/tree/main/website"
          editLink="Edit this page on GitHub"
        >
          {children}
        </Layout>
      </body>
    </html>
  )
}
