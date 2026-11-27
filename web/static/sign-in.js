/* The gate.

   Two modes in one form rather than two pages: the fields are the same,
   and a person who guesses wrong about whether they have an account
   should not have to find a different screen and type it all again. */

const $ = (id) => document.getElementById(id);
let mode = 'in';

const COPY = {
  in: {
    title: 'Sign in',
    sub: 'Where your money is, between the supplier and the shelf.',
    go: 'Sign in',
    swap: 'New here?',
    swapGo: 'Create an account',
    autocomplete: 'current-password',
  },
  up: {
    title: 'Create an account',
    sub: 'One account, then your own organisation inside it.',
    go: 'Create account',
    swap: 'Already have one?',
    swapGo: 'Sign in',
    autocomplete: 'new-password',
  },
};

function paint() {
  const c = COPY[mode];
  $('gate-title').textContent = c.title;
  $('gate-sub').textContent = c.sub;
  $('gate-go').textContent = c.go;
  $('swap-text').textContent = c.swap;
  $('swap').textContent = c.swapGo;
  $('password').setAttribute('autocomplete', c.autocomplete);
  $('pw-hint').hidden = mode !== 'up';
  fail(null);
}

function fail(message) {
  const box = $('gate-error');
  box.hidden = !message;
  box.textContent = message || '';
}

$('swap').addEventListener('click', () => {
  mode = mode === 'in' ? 'up' : 'in';
  paint();            // clears any reported problem, which is right:
                      // switching mode is the person acting on it

  $('email').focus();
});

$('gate-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const email = $('email').value.trim();
  const password = $('password').value;
  if (!email || !password) return fail('Fill in both fields.');

  const go = $('gate-go');
  go.disabled = true;
  go.textContent = mode === 'in' ? 'Signing in…' : 'Creating…';
  fail(null);

  try {
    const r = await fetch(`/auth/sign-${mode === 'in' ? 'in' : 'up'}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'That did not work.');

    if (d.confirm) {
      /* Supabase is set to confirm addresses, so there is no session
         yet. Saying so plainly beats a spinner that never resolves. */
      document.querySelector('.gate-card').innerHTML =
        '<h1>Check your email</h1><p class="gate-sub">We sent a link to ' +
        `<b>${email.replace(/[<>&]/g, '')}</b>. Open it to finish setting ` +
        'up, then come back and sign in.</p>';
      return;
    }
    /* Back where they were, when a screen sent them here because
       their session had run out. Only a same-page fragment is
       honoured -- an open redirect is an open redirect even when the
       only thing that ever sets it is our own error card. */
    const next = new URLSearchParams(location.search).get('next') || '';
    location.href = /^#[\w/?=&.-]*$/.test(next) ? `/${next}` : '/';
  } catch (err) {
    fail(err.message);
    go.disabled = false;
    go.textContent = COPY[mode].go;
  }
});

/* If the server has no Supabase credentials there is nothing to sign
   in to, and letting someone type a password first and then showing
   them a 503 is a waste of their time and tells them nothing. */
async function checkConfigured() {
  try {
    const r = await fetch('/api/config');
    const d = await r.json();
    if (d.auth_configured) return;
    document.querySelector('.gate-card').innerHTML =
      '<h1>Sign-in is not switched on</h1>' +
      '<p class="gate-sub">This deployment has no Supabase credentials, ' +
      'so there are no accounts yet. Set <code>SUPABASE_URL</code> and ' +
      '<code>SUPABASE_ANON_KEY</code> in the environment and redeploy.</p>' +
      '<p class="gate-sub"><a class="linkish" href="/">Open it with sample ' +
      'data instead</a></p>';
  } catch (e) { /* leave the form up; submitting will report the error */ }
}

/* A confirmation link that did not work sends the person here, and
   arriving at a plain sign-in form with no explanation is how someone
   decides the product is broken. Two sources: our own ?problem=, and
   Supabase's own #error=... fragment, which is what the default email
   link produces when it has already been used or has expired. */
const PROBLEMS = {
  expired: 'That confirmation link has expired or had already been '
    + 'used. Links are good once. Sign up again with the same address '
    + 'to get a new one.',
  link: 'That link was incomplete. Open the most recent email and use '
    + 'the link in it.',
  access_denied: 'That confirmation link has expired or had already '
    + 'been used. Sign up again with the same address to get a new one.',
  otp_expired: 'That confirmation link has expired. Sign up again with '
    + 'the same address to get a new one.',
};

function reportProblem() {
  const q = new URLSearchParams(location.search);
  const h = new URLSearchParams(location.hash.replace(/^#/, ''));
  const code = q.get('problem') || h.get('error_code') || h.get('error');
  if (!code) return;
  fail(PROBLEMS[code] || 'That link did not work. Try signing up again.');
  /* Clear it from the address bar so a reload does not re-report a
     problem the person has already dealt with. */
  history.replaceState(null, '', location.pathname);
}

paint();
reportProblem();
$('email').focus();
checkConfigured();
