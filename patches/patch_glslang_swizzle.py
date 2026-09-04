#!/usr/bin/env python3
# Copyright 2026 The Shaderc Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# -*- coding: utf-8 -*-
"""
Fix null-pointer dereferences on glslang swizzle operands.

Background (iOS / Amethyst-iOS, MC 26.3 + LWJGL 3.4.x).

Two distinct crashes, both rooted in callers feeding the result of
getAsAggregate() straight into a reference without checking for null:

  (1) TParseContext::lValueErrorCheck  -- ParseHelper.cpp
      C  [libshaderc.dylib+0x98bec]  ...lValueErrorCheck+0x1b4
      +1ac  blr  x8      ; rightNode->getAsAggregate()
      +1b0  mov  x22, x0 ; aggrNode = result
      +1b4  ldr  x8, [x0]; <-- CRASH, aggrNode == nullptr
      +1bc  blr  x8      ; aggrNode->getSequence()

  (2) TGlslangToSpvTraverser::convertSwizzle -- SPIRV/GlslangToSpv.cpp
      C  [libshaderc.dylib+0x16db94]  ...convertSwizzle+0x20
      +0x10  ldur x0, [x29,#-0x10]  ; the TIntermAggregate reference
      +0x14  ldr  x8, [x0]          ; <-- CRASH, x0 == nullptr
      +0x18  ldr  x8, [x8, #0x1a0]
      +0x1c  blr  x8                ; getSequence()

Both call sites look like this:

    convertSwizzle(*node->getRight()->getAsAggregate(), swizzle);

When the swizzle's right operand is not a TIntermAggregate, getAsAggregate()
returns nullptr, the dereference binds a reference to null, and the first
getSequence() call faults. The identical pattern appears in
createInvertedSwizzle().

The parser normally builds the swizzle operand as a TIntermAggregate of
constant unions, but a single-component swizzle can also arrive as a bare
TIntermConstantUnion, so convertSwizzleSafe() handles that shape explicitly
rather than merely guarding -- otherwise the guard would trade a crash for
silently wrong SPIR-V.

Verified against the glslang revision pinned in shaderc's DEPS
(09c541ee5b22bbac307987b50d86ec2b4f683d75): upstream KhronosGroup/glslang
main, the MobileGL-Dev fork and that pin all lack every one of these checks.

This script is idempotent and fails loudly if any anchor is missing, so a
silent no-op can never masquerade as a successful patch.
"""

import os
import sys

GLSLANG_TO_SPV = os.path.join("SPIRV", "GlslangToSpv.cpp")
PARSE_HELPER = os.path.join("glslang", "MachineIndependent", "ParseHelper.cpp")

MARKER = "convertSwizzleSafe"


class PatchError(Exception):
    pass


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except IOError as exc:
        raise PatchError("cannot read %s: %s" % (path, exc))


def write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def replace_once(text, old, new, path, label):
    # Check the patched form first: several anchors are prefixes of their
    # replacement (the declaration gains a sibling line), so testing the
    # unpatched form first would match again and duplicate the insertion.
    if new in text:
        return text, "already applied"
    if old not in text:
        raise PatchError(
            "anchor not found for %s in %s; glslang source layout changed"
            % (label, path))
    return text.replace(old, new, 1), "applied"


# --- SPIRV/GlslangToSpv.cpp -------------------------------------------------

# 1. Declare the safe helper next to convertSwizzle.
DECL_OLD = """    void convertSwizzle(const glslang::TIntermAggregate&, std::vector<unsigned>& swizzle);
"""
DECL_NEW = """    void convertSwizzle(const glslang::TIntermAggregate&, std::vector<unsigned>& swizzle);
    bool convertSwizzleSafe(const glslang::TIntermTyped* right, std::vector<unsigned>& swizzle,
        const char* context);
"""

# 2. Harden convertSwizzle itself and add the helper.
DEF_OLD = """// Convert a glslang AST swizzle node to a swizzle vector for building SPIR-V.
void TGlslangToSpvTraverser::convertSwizzle(const glslang::TIntermAggregate& node, std::vector<unsigned>& swizzle)
{
    const glslang::TIntermSequence& swizzleSequence = node.getSequence();
    for (int i = 0; i < (int)swizzleSequence.size(); ++i)
        swizzle.push_back(swizzleSequence[i]->getAsConstantUnion()->getConstArray()[0].getIConst());
}
"""
DEF_NEW = """// Convert a glslang AST swizzle node to a swizzle vector for building SPIR-V.
void TGlslangToSpvTraverser::convertSwizzle(const glslang::TIntermAggregate& node, std::vector<unsigned>& swizzle)
{
    const glslang::TIntermSequence& swizzleSequence = node.getSequence();
    for (int i = 0; i < (int)swizzleSequence.size(); ++i) {
        const glslang::TIntermTyped* selector = swizzleSequence[i]->getAsTyped();
        const glslang::TIntermConstantUnion* constant =
            selector ? selector->getAsConstantUnion() : nullptr;
        // getConstArray() returns a reference, never null; its backing vector
        // can be unallocated, which size() reports as 0.
        if (constant == nullptr || constant->getConstArray().size() == 0)
            continue;
        swizzle.push_back(constant->getConstArray()[0].getIConst());
    }
}

// Resolve the right operand of a glslang swizzle node into a swizzle vector.
//
// The operand is normally a TIntermAggregate of constant unions, but a
// single-component swizzle can also arrive as a bare TIntermConstantUnion.
// Binding a null aggregate to convertSwizzle()'s reference parameter crashes on
// the first getSequence() call, so both shapes are handled here and anything
// else degrades to "missing functionality" instead of a segfault.
bool TGlslangToSpvTraverser::convertSwizzleSafe(const glslang::TIntermTyped* right,
    std::vector<unsigned>& swizzle, const char* context)
{
    swizzle.clear();
    (void)context;

    if (right == nullptr) {
        logger->missingFunctionality("null swizzle operand");
        return false;
    }

    if (const glslang::TIntermAggregate* aggregate = right->getAsAggregate()) {
        convertSwizzle(*aggregate, swizzle);
        if (swizzle.empty())
            logger->missingFunctionality("empty swizzle operand");
        return !swizzle.empty();
    }

    if (const glslang::TIntermConstantUnion* constant = right->getAsConstantUnion()) {
        if (constant->getConstArray().size() > 0) {
            swizzle.push_back(constant->getConstArray()[0].getIConst());
            return true;
        }
    }

    logger->missingFunctionality("unsupported swizzle operand");
    return false;
}
"""

# 3. visitBinary, case EOpVectorSwizzle.
SITE_OLD = """            convertSwizzle(*node->getRight()->getAsAggregate(), swizzle);
"""
SITE_NEW = """            if (!convertSwizzleSafe(node->getRight(), swizzle, "EOpVectorSwizzle")) {
                logger->missingFunctionality("vector swizzle operand");
                return true;
            }
"""

# 4. createInvertedSwizzle.
INVERTED_OLD = """    std::vector<unsigned> swizzle;
    convertSwizzle(*node.getAsBinaryNode()->getRight()->getAsAggregate(), swizzle);
    return builder.createRvalueSwizzle(precision, convertGlslangToSpvType(node.getType()), parentResult, swizzle);
"""
INVERTED_NEW = """    std::vector<unsigned> swizzle;
    const glslang::TIntermBinary* binaryNode = node.getAsBinaryNode();
    if (binaryNode == nullptr || !convertSwizzleSafe(binaryNode->getRight(), swizzle, "createInvertedSwizzle")) {
        logger->missingFunctionality("inverted swizzle operand");
        return parentResult;
    }
    return builder.createRvalueSwizzle(precision, convertGlslangToSpvType(node.getType()), parentResult, swizzle);
"""

# --- glslang/MachineIndependent/ParseHelper.cpp -----------------------------

# 5. Guard against a null node at function entry.
LVALUE_ENTRY_OLD = """bool TParseContext::lValueErrorCheck(const TSourceLoc& loc, const char* op, TIntermTyped* node)
{
    TIntermBinary* binaryNode = node->getAsBinaryNode();"""
LVALUE_ENTRY_NEW = """bool TParseContext::lValueErrorCheck(const TSourceLoc& loc, const char* op, TIntermTyped* node)
{
    if (node == nullptr)
        return true;

    TIntermBinary* binaryNode = node->getAsBinaryNode();"""

# 6. Guard the unguarded getAsAggregate() dereference for EOpVectorSwizzle.
LVALUE_OLD = """                TIntermTyped* rightNode = binaryNode->getRight();
                TIntermAggregate *aggrNode = rightNode->getAsAggregate();

                for (TIntermSequence::iterator p = aggrNode->getSequence().begin();"""
LVALUE_NEW = """                TIntermTyped* rightNode = binaryNode->getRight();
                TIntermAggregate *aggrNode = rightNode ? rightNode->getAsAggregate() : nullptr;
                if (aggrNode == nullptr)
                    return errorReturn;

                for (TIntermSequence::iterator p = aggrNode->getSequence().begin();"""


def patch_glslang_to_spv(root):
    path = os.path.join(root, GLSLANG_TO_SPV)
    text = read(path)
    for old, new, label in (
        (DECL_OLD, DECL_NEW, "convertSwizzleSafe declaration"),
        (DEF_OLD, DEF_NEW, "convertSwizzle definition"),
        (SITE_OLD, SITE_NEW, "visitBinary swizzle site"),
        (INVERTED_OLD, INVERTED_NEW, "createInvertedSwizzle site"),
    ):
        text, status = replace_once(text, old, new, GLSLANG_TO_SPV, label)
        print("[patch] %-38s %s" % (label, status))
    write(path, text)


def patch_parse_helper(root):
    path = os.path.join(root, PARSE_HELPER)
    text = read(path)
    for old, new, label in (
        (LVALUE_ENTRY_OLD, LVALUE_ENTRY_NEW, "lValueErrorCheck null node"),
        (LVALUE_OLD, LVALUE_NEW, "lValueErrorCheck swizzle"),
    ):
        text, status = replace_once(text, old, new, PARSE_HELPER, label)
        print("[patch] %-38s %s" % (label, status))
    write(path, text)


def main():
    if len(sys.argv) != 2:
        sys.stderr.write("usage: %s <glslang-source-dir>\n" % sys.argv[0])
        return 2
    root = sys.argv[1]
    for sub in (GLSLANG_TO_SPV, PARSE_HELPER):
        if not os.path.isfile(os.path.join(root, sub)):
            sys.stderr.write("ERROR: not found: %s\n" % os.path.join(root, sub))
            return 1

    print("[patch] patching glslang in %s" % root)
    patch_glslang_to_spv(root)
    patch_parse_helper(root)

    # Post-condition: the marker must be present, otherwise the crash site is
    # still live and the build would ship a dylib that segfaults in place.
    patched = read(os.path.join(root, GLSLANG_TO_SPV))
    if MARKER not in patched:
        raise PatchError("post-condition failed: %s missing after patch" % MARKER)
    print("[patch] glslang swizzle patch verified")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PatchError as error:
        sys.stderr.write("ERROR: %s\n" % error)
        sys.exit(1)
