/** @type {import('next').NextConfig} */
const nextConfig = {
  // The FastAPI app owns every /api route. Proxying rather than calling it
  // cross-origin keeps the browser on one origin, so there is no CORS
  // preflight on every interaction during a demo.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.API_BASE ?? "http://127.0.0.1:8000"}/api/:path*`,
      },
    ];
  },
};
export default nextConfig;
