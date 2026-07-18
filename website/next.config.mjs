import nextra from 'nextra'

const withNextra = nextra({
  // Content directory base path — all routes under /docs map to content/ folder
  contentDirBasePath: '/docs',
})

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  images: {
    unoptimized: true,
  },
}

export default withNextra(nextConfig)
