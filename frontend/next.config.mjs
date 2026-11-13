/** @type {import('next').NextConfig} */
const backend = process.env.RR_BACKEND_URL || "http://localhost:8000";
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  images: { unoptimized: true },
  // In production Nginx routes /api to FastAPI; this rewrite serves local development.
  async rewrites() {
    return process.env.NODE_ENV === "production" ? [] : [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};
export default nextConfig;
