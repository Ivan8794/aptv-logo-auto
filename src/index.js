export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/") {
      return new Response(
        "APTV EPG logo index is available at /logos-map.json\\n",
        {
          headers: {
            "content-type": "text/plain; charset=utf-8",
            "cache-control": "public, max-age=300",
          },
        },
      );
    }

    if (url.pathname !== "/logos-map.json") {
      return new Response("Not found\\n", { status: 404 });
    }

    return env.ASSETS.fetch(request);
  },
};
