import { cp, mkdir, rm } from 'node:fs/promises';
await rm('dist', { recursive: true, force: true });
await mkdir('dist');
await cp('index.html', 'dist/index.html');
await cp('src/styles.css', 'dist/styles.css');
await cp('src/app.js', 'dist/app.js');
console.log('Built static site in dist/');
