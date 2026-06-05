const vertexShader = /* glsl*/ `
    attribute vec2 vertex_position;
    void main(void) {
        gl_Position = vec4(vertex_position, 0.0, 1.0);
    }
`;

const fragmentShader = /* glsl*/ `
    uniform sampler2D srcTexture;
    uniform vec2 dstSize;

    void main(void) {
        vec2 srcSize = vec2(textureSize(srcTexture, 0));
        vec2 drawSize = srcSize * min(dstSize.x / srcSize.x, dstSize.y / srcSize.y);
        vec2 drawMin = (dstSize - drawSize) * 0.5;
        vec2 dst = gl_FragCoord.xy - drawMin;

        if (any(lessThan(dst, vec2(0.0))) || any(greaterThanEqual(dst, drawSize))) {
            gl_FragColor = vec4(0.0);
            return;
        }

        ivec2 texel = ivec2(floor(dst / drawSize * srcSize));
        gl_FragColor = texelFetch(srcTexture, texel, 0);
    }
`;

export { vertexShader, fragmentShader };
