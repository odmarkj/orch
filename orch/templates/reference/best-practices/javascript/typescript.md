# TypeScript Advanced Best Practices

TypeScript's advanced type system enables compile-time safety guarantees that eliminate entire categories of runtime errors. This reference covers the patterns and techniques needed to build type-safe libraries, APIs, and applications beyond basic type annotations.

Covers: advanced generics, conditional types, mapped types, template literal types, utility types, strict mode configuration, type-safe event emitters, type-safe API clients, builder patterns, discriminated unions, type guards, and type testing.

---

## Generics

- **Constrain generic parameters** -- `<T extends HasLength>` prevents invalid usage at the call site rather than at runtime
- **Multiple type parameters for composition** -- `function merge<T, U>(a: T, b: U): T & U` preserves both input types in the result
- **Infer over annotate** -- let TypeScript infer generic types from arguments; explicit annotations only when inference fails
- **Default type parameters** -- `<T = string>` provides sensible defaults while keeping the generic flexible for overrides
- **Generic classes with constraints** -- `class Store<T extends { id: string }>` ensures stored items are identifiable

## Conditional Types

- **Basic conditional** -- `type IsString<T> = T extends string ? true : false` enables type-level branching
- **infer keyword for extraction** -- `T extends (...args: any[]) => infer R ? R : never` extracts return types, parameters, or array elements
- **Distributive behavior** -- `ToArray<string | number>` distributes to `string[] | number[]` when `T extends any ? T[] : never`
- **Nested conditionals for type mapping** -- chain `T extends string ? "string" : T extends number ? "number" : "other"` for type-level pattern matching
- **Avoid deeply nested conditionals** -- more than 3-4 levels slows the compiler; extract into named helper types

## Mapped Types

- **Transform all properties** -- `{ [P in keyof T]: NewType }` creates Readonly, Partial, Required variants systematically
- **Key remapping with as** -- `[K in keyof T as \`get${Capitalize<string & K>}\`]` generates getter signatures from object shapes
- **Filter properties by type** -- `[K in keyof T as T[K] extends U ? K : never]: T[K]` picks only properties matching a type
- **Deep recursive types** -- `DeepReadonly<T>` and `DeepPartial<T>` traverse nested objects; guard against Function types to preserve callable properties
- **Modifier removal** -- `-readonly` and `-?` remove readonly and optional modifiers respectively

## Template Literal Types

- **String pattern types** -- `type EventHandler = \`on${Capitalize<EventName>}\`` generates "onClick" | "onFocus" | "onBlur" from a union
- **Built-in string utilities** -- `Uppercase`, `Lowercase`, `Capitalize`, `Uncapitalize` transform string literal types
- **Recursive path types** -- `type Path<T> = { [K in keyof T]: K | \`${K}.${Path<T[K]>}\` }[keyof T]` generates "server.host" | "server.port" from nested config shapes
- **Branded types for safety** -- `type UserId = string & { __brand: 'UserId' }` prevents mixing user IDs with other string identifiers

## Utility Types

- **Pick and Omit for subsetting** -- `Pick<User, 'name' | 'email'>` and `Omit<User, 'password'>` create focused types from larger interfaces
- **Exclude and Extract on unions** -- `Exclude<'a' | 'b' | 'c', 'a'>` yields `'b' | 'c'`; Extract does the inverse
- **NonNullable** -- `NonNullable<string | null | undefined>` strips null and undefined from union types
- **Record for dictionaries** -- `Record<string, User>` is clearer than `{ [key: string]: User }` and supports literal key unions
- **ReturnType and Parameters** -- extract function signatures without importing the function itself; useful for third-party library types
- **Awaited** -- `Awaited<Promise<Promise<number>>>` recursively unwraps nested promises to the inner type

## Discriminated Unions

- **Tag with a literal property** -- `{ status: 'success'; data: T } | { status: 'error'; error: string }` enables exhaustive switch narrowing
- **Exhaustive checks with never** -- `default: const _exhaustive: never = state` catches unhandled union members at compile time
- **State machines as unions** -- model application states as `{ type: 'idle' } | { type: 'loading' } | { type: 'success'; data: T }` for type-safe transitions
- **Event-driven systems** -- discriminated unions for events enable type-safe dispatch and reducer patterns

## Type-Safe Design Patterns

- **Type-safe event emitter** -- `TypedEventEmitter<{ 'user:created': { id: string; name: string } }>` constrains both event names and payload shapes
- **Type-safe API client** -- map route paths to request/response types with `EndpointConfig` records; infer params, body, and response per method
- **Builder pattern with completeness tracking** -- generic state parameter tracks which required fields are set; `build()` only callable when all required fields present
- **Type-safe form validation** -- `FieldValidation<T>` maps form field names to validation rules typed to each field's value type
- **Branded primitives** -- `type Email = string & { __brand: 'email' }` with a validation constructor prevents raw strings from bypassing validation

## Type Guards and Assertions

- **Custom type guards** -- `function isString(value: unknown): value is string` narrows types in conditional branches
- **Composable guards** -- `isArrayOf(data, isString)` builds complex guards from simple predicates
- **Assertion functions** -- `function assertIsString(value: unknown): asserts value is string` narrows in the calling scope after the call
- **Prefer type guards over type assertions** -- `as` casts bypass the type checker; guards prove correctness

## Strict Mode Configuration

- **Enable all strict flags** -- `strict: true` in tsconfig enables strictNullChecks, noImplicitAny, strictFunctionTypes, and more
- **noUncheckedIndexedAccess** -- array and record index access returns `T | undefined`, catching out-of-bounds access at compile time
- **exactOptionalProperties** -- distinguishes `{ key?: string }` (missing) from `{ key: undefined }` (present but undefined)
- **Incremental adoption** -- enable strict flags one at a time when migrating; fix errors per flag before enabling the next

## Type Testing

- **AssertEqual utility** -- `type AssertEqual<T, U> = [T] extends [U] ? [U] extends [T] ? true : false : false` verifies type equivalence in test files
- **Expect error patterns** -- ensure invalid type combinations produce compile errors using `@ts-expect-error` comments
- **Test complex types** -- write type-level tests alongside runtime tests to catch regressions in utility types and generics

## Performance and Pitfalls

- **Avoid excessive any** -- use `unknown` and narrow; `any` defeats TypeScript's purpose and propagates unsafety
- **Limit recursive type depth** -- deeply recursive conditional types cause "type instantiation is excessively deep" errors; add recursion guards
- **Cache complex type computations** -- extract frequently used conditional types into named aliases to reduce compiler work
- **Prefer interface for object shapes** -- interfaces produce better error messages and support declaration merging
- **Use type for unions and intersections** -- type aliases are more flexible for computed and conditional types
- **Avoid circular type references** -- restructure types or use lazy evaluation patterns to prevent compiler errors
- **const assertions preserve literals** -- `as const` on object literals preserves exact string and number types instead of widening
- **Readonly by default** -- mark properties as `readonly` to prevent accidental mutation; remove only when mutation is intentional

## Declaration Files and Library Authoring

- **Generate .d.ts for library consumers** -- `declaration: true` in tsconfig emits type declarations alongside JavaScript output
- **DefinitelyTyped for untyped libraries** -- `@types/package` provides community-maintained type definitions for JavaScript packages
- **Module augmentation** -- `declare module 'express' { interface Request { user?: User } }` extends third-party types without forking
- **Triple-slash directives for ambient types** -- `/// <reference types="node" />` includes global type definitions in non-module files
- **Export type vs export** -- `export type { Foo }` ensures the export is erased at runtime; prevents accidental runtime dependency on types

## Decorators and Metadata

- **Stage 3 decorators (TC39)** -- standard decorator syntax replacing experimental decorators; use `experimentalDecorators: false` for new projects
- **Decorator factories for configuration** -- `@Validate({ min: 0, max: 100 })` returns a decorator configured with parameters
- **reflect-metadata for runtime type info** -- enables frameworks like NestJS and TypeORM to inspect types at runtime for dependency injection

## Migration Strategies

- **Gradual adoption with allowJs** -- enable `allowJs: true` and `checkJs: true` to typecheck JavaScript files incrementally
- **Start with strict: false** -- enable strict flags one at a time; fix all errors per flag before enabling the next
- **JSDoc annotations as stepping stone** -- `@param {string} name` and `@returns {number}` provide type checking without renaming to .ts
- **Rename .js to .ts incrementally** -- convert one module at a time starting from leaf modules with no dependents
- **Use unknown at boundaries** -- external data (API responses, user input, JSON.parse) enters as `unknown` and must be validated before use

## Framework Integration

- **React with TypeScript** -- `FC<Props>` or explicit return types; use discriminated unions for component state
- **Express with typed middleware** -- augment the Express Request interface for auth, user context, and request metadata
- **Zod for runtime + compile-time safety** -- `z.infer<typeof schema>` derives TypeScript types from Zod schemas; single source of truth for validation
- **Prisma for type-safe database access** -- generated types from schema ensure queries match database structure at compile time
