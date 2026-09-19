// Allow the site's own browser verification to finish; never bypass a challenge.
const challengeTitle = title => /captcha|antibot|access denied/i.test(title);

export async function settleSourcePage(page, response, timeout = 30000) {
  if (response?.status() !== 403 && !challengeTitle(await page.title())) return response;
  let current = response;
  const navigation = next => {
    if (next.request().isNavigationRequest() && next.frame() === page.mainFrame()) current = next;
  };
  page.on('response', navigation);
  try {
    await page.waitForFunction(() =>
      document.title && !/captcha|antibot|access denied/i.test(document.title)
      && document.querySelector('[data-widget="webProductMainWidget"], [id^="state-tileGridDesktop-"]'),
    undefined, {timeout});
    if (current?.status() === 403 || challengeTitle(await page.title())) throw Error('browser_access_challenge');
    return current;
  } catch (error) {
    if (error.name === 'TimeoutError' || error.message === 'browser_access_challenge') throw Error('browser_access_challenge');
    throw error;
  } finally {
    page.off('response', navigation);
  }
}
