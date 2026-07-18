import { generateStaticParamsFor, importPage } from 'nextra/pages'
import { useMDXComponents as getMDXComponents } from '../../../mdx-components'

// With contentDirBasePath: '/docs', the content/ dir maps to /docs/* routes.
// This catch-all lives at app/docs/[[...mdxPath]] so mdxPath holds the
// segments AFTER /docs. importPage receives just those tail segments.
export const generateStaticParams = generateStaticParamsFor('mdxPath')

const Wrapper = getMDXComponents().wrapper

export async function generateMetadata(props) {
  const params = await props.params
  const { metadata } = await importPage(params.mdxPath ?? [])
  return metadata
}

export default async function Page(props) {
  const params = await props.params
  const { default: MDXContent, metadata, sourceCode, toc } = await importPage(params.mdxPath ?? [])
  return (
    <Wrapper metadata={metadata} sourceCode={sourceCode} toc={toc}>
      <MDXContent {...props} params={params} />
    </Wrapper>
  )
}
