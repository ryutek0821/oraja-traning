"use strict";

// Incremental SHA-256 for large File objects. WebCrypto's digest API requires
// one contiguous ArrayBuffer, so the upload UI uses this small streaming
// implementation to keep memory bounded to one 1 MiB slice.
const K = new Uint32Array([
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2,
]);

const rotr = (value, bits) => (value >>> bits) | (value << (32 - bits));

export class Sha256 {
  constructor() {
    this.state = new Uint32Array([0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]);
    this.buffer = new Uint8Array(64);
    this.bufferLength = 0;
    this.bytesHashed = 0n;
    this.finished = false;
  }

  update(input) {
    if (this.finished) throw new Error("sha256_already_finalized");
    const data = input instanceof Uint8Array ? input : new Uint8Array(input);
    this.bytesHashed += BigInt(data.byteLength);
    let offset = 0;
    while (offset < data.byteLength) {
      const take = Math.min(64 - this.bufferLength, data.byteLength - offset);
      this.buffer.set(data.subarray(offset, offset + take), this.bufferLength);
      this.bufferLength += take;
      offset += take;
      if (this.bufferLength === 64) {
        this.compress(this.buffer);
        this.bufferLength = 0;
      }
    }
    return this;
  }

  compress(block) {
    const words = new Uint32Array(64);
    const view = new DataView(block.buffer, block.byteOffset, 64);
    for (let index = 0; index < 16; index += 1) words[index] = view.getUint32(index * 4, false);
    for (let index = 16; index < 64; index += 1) {
      const x = words[index - 15];
      const y = words[index - 2];
      const s0 = rotr(x, 7) ^ rotr(x, 18) ^ (x >>> 3);
      const s1 = rotr(y, 17) ^ rotr(y, 19) ^ (y >>> 10);
      words[index] = (words[index - 16] + s0 + words[index - 7] + s1) >>> 0;
    }
    let [a,b,c,d,e,f,g,h] = this.state;
    for (let index = 0; index < 64; index += 1) {
      const s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const choice = (e & f) ^ (~e & g);
      const t1 = (h + s1 + choice + K[index] + words[index]) >>> 0;
      const s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (s0 + majority) >>> 0;
      h=g; g=f; f=e; e=(d+t1)>>>0; d=c; c=b; b=a; a=(t1+t2)>>>0;
    }
    const values = [a,b,c,d,e,f,g,h];
    for (let index = 0; index < 8; index += 1) this.state[index] = (this.state[index] + values[index]) >>> 0;
  }

  digestHex() {
    if (this.finished) throw new Error("sha256_already_finalized");
    this.finished = true;
    const bitLength = this.bytesHashed * 8n;
    this.buffer[this.bufferLength++] = 0x80;
    if (this.bufferLength > 56) {
      this.buffer.fill(0, this.bufferLength);
      this.compress(this.buffer);
      this.bufferLength = 0;
    }
    this.buffer.fill(0, this.bufferLength, 56);
    const view = new DataView(this.buffer.buffer);
    view.setUint32(56, Number((bitLength >> 32n) & 0xffffffffn), false);
    view.setUint32(60, Number(bitLength & 0xffffffffn), false);
    this.compress(this.buffer);
    return [...this.state].map((value) => value.toString(16).padStart(8, "0")).join("");
  }
}

export async function sha256Blob(blob, chunkSize = 1024 * 1024) {
  const hasher = new Sha256();
  for (let offset = 0; offset < blob.size; offset += chunkSize) {
    hasher.update(await blob.slice(offset, offset + chunkSize).arrayBuffer());
  }
  return hasher.digestHex();
}
