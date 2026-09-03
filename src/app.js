const menuButton = document.querySelector('.menu-button');
const nav = document.querySelector('.nav-links');
menuButton.addEventListener('click', () => {
  const open = nav.classList.toggle('open');
  menuButton.setAttribute('aria-expanded', String(open));
  menuButton.textContent = open ? '×' : '☰';
});
nav.addEventListener('click', () => {
  nav.classList.remove('open');
  menuButton.setAttribute('aria-expanded', 'false');
  menuButton.textContent = '☰';
});
document.querySelector('[data-scroll="path"]').addEventListener('click', () => document.querySelector('#path').scrollIntoView({ behavior: 'smooth' }));
document.querySelectorAll('.faq-item button').forEach((button) => button.addEventListener('click', () => {
  const item = button.closest('.faq-item');
  const wasOpen = item.classList.contains('active');
  document.querySelectorAll('.faq-item').forEach((other) => { other.classList.remove('active'); other.querySelector('button').setAttribute('aria-expanded', 'false'); });
  if (!wasOpen) { item.classList.add('active'); button.setAttribute('aria-expanded', 'true'); }
}));
document.querySelector('.join form').addEventListener('submit', (event) => {
  event.preventDefault();
  event.currentTarget.hidden = true;
  document.querySelector('.success').hidden = false;
});
