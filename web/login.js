const form = document.querySelector('#loginForm');
const errorMessage = document.querySelector('#loginError');
const submitButton = form.querySelector('button[type="submit"]');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorMessage.textContent = '';
  submitButton.disabled = true;
  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: form.elements.username.value,
        password: form.elements.password.value,
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || 'Sign in failed');
    window.location.replace('/');
  } catch (error) {
    errorMessage.textContent = error.message;
    document.querySelector('#password').select();
  } finally {
    submitButton.disabled = false;
  }
});
