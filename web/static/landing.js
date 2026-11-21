/* Confirmation lands here with the session in the fragment.

   The fragment never reaches a server -- browsers do not send it -- so
   it has to be read in the page and handed over deliberately. Three
   things happen, in this order: read it, remove it from the address
   bar so a back button or a screenshot cannot resurrect it, then post
   it to the server, which verifies the signature before issuing a
   cookie the page itself cannot read.

   This exists because Supabase's built-in email template cannot be
   edited without custom SMTP. With a custom template, /auth/confirm is
   the better door: it keeps the token out of the URL entirely. */

const $ = (id) => document.getElementById(id);

function show(title, body, offerSignIn) {
  $('h').textContent = title;
  $('p').textContent = body;
  $('link').hidden = !offerSignIn;
}

async function run() {
  const hash = new URLSearchParams(location.hash.replace(/^#/, ''));
  const query = new URLSearchParams(location.search);

  const errorCode = hash.get('error_code') || hash.get('error')
    || query.get('error_code') || query.get('error');
  const access = hash.get('access_token');
  const refresh = hash.get('refresh_token') || '';

  // Out of the address bar before anything else, including before the
  // network call: whatever happens next, it should not still be there.
  if (location.hash || location.search) {
    history.replaceState(null, '', location.pathname);
  }

  if (errorCode) {
    location.replace(`/sign-in?problem=${encodeURIComponent(errorCode)}`);
    return;
  }
  if (!access) {
    show('That link did not carry a session',
         'It may have already been used. Links work once. Sign up again '
         + 'with the same address to get a new one.', true);
    return;
  }

  try {
    const r = await fetch('/auth/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ access_token: access, refresh_token: refresh }),
    });
    if (!r.ok) throw new Error('rejected');
    location.replace('/');
  } catch (e) {
    show('That link did not work',
         'It may have expired. Sign up again with the same address to get '
         + 'a new one.', true);
  }
}

run();
