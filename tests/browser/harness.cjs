const base = process.env.APP_BASE_URL;
const session = process.env.CI_SESSION_COOKIE;
if (!base || !session || new URL(base).hostname !== "127.0.0.1") {
  throw new Error("Start the isolated Compose stack and set APP_BASE_URL and CI_SESSION_COOKIE");
}

function watchPage(page) {
  const failures = [];
  page.on("pageerror", (error) => failures.push(error.message));
  page.on("requestfailed", (request) => {
    if (request.failure().errorText !== "net::ERR_ABORTED") {
      failures.push(`${request.url()}: ${request.failure().errorText}`);
    }
  });
  page.on("response", (response) => {
    if (response.status() >= 400) failures.push(`${response.status()} ${response.url()}`);
  });
  return failures;
}

async function signIn(page) {
  await page.context().addCookies([{ name: "session", value: session, url: base, httpOnly: true }]);
}

function nextResponse(page, path, method = "GET") {
  return page.waitForResponse((response) => {
    const pathname = new URL(response.url()).pathname;
    const matches = typeof path === "string" ? pathname === path : path.test(pathname);
    return matches && response.request().method() === method;
  });
}

module.exports = { base, nextResponse, signIn, watchPage };
