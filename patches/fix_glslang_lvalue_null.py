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
Fix a null-pointer dereference in glslang's TParseContext::lValueErrorCheck.

Background (iOS / Amethyst-iOS, MC 26.3 + LWJGL 3.4.x):

  SIGSEGV in libshaderc.dylib
  C  [libshaderc.dylib+0x98bec]  glslang::TParseContext::lValueErrorCheck+0x1b4

Disassembly of the crashing frame shows:

  +1a0  blr   x8        ; binaryNode->getRight()
  +1ac  blr   x8        ; rightNode->getAsAggregate()
  +1b0  mov   x22, x0   ; aggrNode = result
  +1b4  ldr   x8, [x0]  ; <-- CRASH: dereferences aggrNode, x0 == nullptr
  +1bc  blr   x8        ; aggrNode->getSequence()

which maps to the EOpVectorSwizzle branch:

  TIntermTyped* rightNode = binaryNode->getRight();
  TIntermAggregate* aggrNode = rightNode->getAsAggregate();
  for (auto p = aggrNode->getSequence().begin(); ...)   // no null check

getAsAggregate() returns nullptr whenever the right-hand node of a swizzle
l-value is not an aggregate node, so the very next line dereferences null.
Verified against upstream KhronosGroup/glslang (main), the MobileGL-Dev fork,
and the revision pinned in shaderc's DEPS: all three lack the check.

This script is idempotent and fails loudly if either anchor is not found,
so a silent no-op can never masquerade as a successful patch.
"""

import os
import sys

REL = "glslang/MachineIndependent/ParseHelper.cpp"

# --- patch 1: guard the whole function against a null node -------------------
OLD_HEAD = """bool TParseContext::lValueErrorCheck(const TSourceLoc& loc, const char* op, TIntermTyped* node)
{
    TIntermBinary* binaryNode = node->getAsBinaryNode();"""

NEW_HEAD = """bool TParseContext::lValueErrorCheck(const TSourceLoc& loc, const char* op, TIntermTyped* node)
{
    if (node == nullptr)
        return true;

    TIntermBinary* binaryNode = node->getAsBinaryNode();"""

# --- patch 2: the actual crash site ------------------------------------------
OLD_SWIZZLE = """                TIntermTyped* rightNode = binaryNode->getRight();
                TIntermAggregate *aggrNode = rightNode->getAsAggregate();

                for (TIntermSequence::iterator p = aggrNode->getSequence().begin();"""

NEW_SWIZZLE = """                TIntermTyped* rightNode = binaryNode->getRight();
                TIntermAggregate *aggrNode = rightNode ? rightNode->getAsAggregate() : nullptr;
                if (aggrNode == nullptr)
                    return errorReturn;

                for (TIntermSequence::iterator p = aggrNode->getSequence().begin();"""


def main():
    if len(sys.argv) != 2:
        sys.stderr.write("usage: %s <glslang-source-dir>\n" % sys.argv[0])
        return 2
    path = os.path.join(sys.argv[1], REL)
    if not os.path.isfile(path):
        sys.stderr.write("ERROR: not found: %s\n" % path)
        return 1

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    already = (NEW_HEAD in src) and (NEW_SWIZZLE in src)
    if already:
        print("[patch] %s already patched, skipping" % REL)
        return 0

    if OLD_HEAD not in src:
        sys.stderr.write("ERROR: anchor 1 (function head) not found in %s\n" % REL)
        return 1
    if OLD_SWIZZLE not in src:
        sys.stderr.write("ERROR: anchor 2 (swizzle branch) not found in %s\n" % REL)
        return 1

    src = src.replace(OLD_HEAD, NEW_HEAD, 1)
    src = src.replace(OLD_SWIZZLE, NEW_SWIZZLE, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    # verify the write actually landed
    with open(path, "r", encoding="utf-8") as f:
        check = f.read()
    if NEW_HEAD not in check or NEW_SWIZZLE not in check:
        sys.stderr.write("ERROR: patch did not persist\n")
        return 1

    print("[patch] %s: applied 2 guards (null node, null aggrNode)" % REL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
